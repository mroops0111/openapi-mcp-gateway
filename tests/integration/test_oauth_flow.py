"""Integration tests for the full OAuth2 flow through GatewayOAuthProvider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from openapi_mcp_gateway.auth.provider import GatewayOAuthProvider
from openapi_mcp_gateway.stores.memory import MemoryTokenStore


@pytest.fixture
def store():
    return MemoryTokenStore()


@pytest.fixture
def provider(store):
    return GatewayOAuthProvider(
        store=store,
        upstream_auth_url='https://auth.example.com/authorize',
        upstream_token_url='https://auth.example.com/token',
        client_id='gateway-client-id',
        client_secret='gateway-client-secret',
        callback_url='http://localhost:8000/petstore/auth/callback',
        scopes=['read', 'write'],
        prefix='petstore',
    )


@pytest.fixture
def mcp_client_info():
    return OAuthClientInformationFull(
        client_id='mcp-client-123',
        client_secret='mcp-secret',
        redirect_uris=[AnyHttpUrl('http://localhost:3000/callback')],
    )


class TestClientRegistration:
    async def test_register_and_get(self, provider, mcp_client_info):
        await provider.register_client(mcp_client_info)
        retrieved = await provider.get_client('mcp-client-123')
        assert retrieved is not None
        assert retrieved.client_id == 'mcp-client-123'

    async def test_get_nonexistent_client(self, provider):
        result = await provider.get_client('nonexistent')
        assert result is None

    async def test_register_without_client_id_raises(self, provider):
        client = OAuthClientInformationFull(
            client_id='',
            redirect_uris=[AnyHttpUrl('http://localhost/cb')],
        )
        with pytest.raises(ValueError, match='client_id'):
            await provider.register_client(client)


class TestAuthorize:
    async def test_authorize_returns_upstream_url(self, provider, mcp_client_info):
        await provider.register_client(mcp_client_info)
        params = AuthorizationParams(
            state='test-state',
            scopes=['read'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-abc',
        )
        url = await provider.authorize(mcp_client_info, params)
        assert url.startswith('https://auth.example.com/authorize?')
        assert 'client_id=gateway-client-id' in url
        assert 'state=test-state' in url
        assert 'scope=read+write' in url


class TestFullOAuthLifecycle:
    """Test the complete flow: authorize → callback → exchange → tool call → refresh → revoke."""

    async def test_full_flow(self, provider, mcp_client_info, store):
        # 1. Register client
        await provider.register_client(mcp_client_info)

        # 2. Authorize → get upstream URL
        params = AuthorizationParams(
            state='state-xyz',
            scopes=['read', 'write'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-123',
        )
        upstream_url = await provider.authorize(mcp_client_info, params)
        assert 'state=state-xyz' in upstream_url

        # 3. Simulate upstream callback
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'access_token': 'upstream-access-token',
            'refresh_token': 'upstream-refresh-token',
            'expires_in': 3600,
        }

        with patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=mock_response):
            redirect = await provider.handle_upstream_callback('upstream-code', 'state-xyz')

        assert 'code=mcp_' in redirect
        # Extract MCP auth code from redirect
        mcp_auth_code = redirect.split('code=')[1].split('&')[0]

        # 4. Load and exchange authorization code
        auth_code = await provider.load_authorization_code(mcp_client_info, mcp_auth_code)
        assert auth_code is not None
        assert auth_code.client_id == 'mcp-client-123'

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)
        assert token.access_token.startswith('mcp_')
        assert token.refresh_token.startswith('mcp_refresh_')
        assert token.expires_in == 3600

        # 5. Verify token can be loaded
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.client_id == 'mcp-client-123'

        # 6. Verify upstream token mapping
        upstream = await store.get_mapping('mcp_access_token', token.access_token, 'api_access_token')
        assert upstream == 'upstream-access-token'

        # 7. Refresh token
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        new_token = await provider.exchange_refresh_token(mcp_client_info, refresh, ['read', 'write'])
        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token

        # Old tokens should be deleted
        assert await provider.load_access_token(token.access_token) is None

        # New token should map to upstream
        new_upstream = await store.get_mapping('mcp_access_token', new_token.access_token, 'api_access_token')
        assert new_upstream == 'upstream-access-token'

        # 8. Revoke
        new_access = await provider.load_access_token(new_token.access_token)
        await provider.revoke_token(new_access)

        assert await provider.load_access_token(new_token.access_token) is None
        assert await provider.load_refresh_token(mcp_client_info, new_token.refresh_token) is None


class TestGetApiAccessToken:
    async def test_get_api_access_token(self, provider, store):
        """Simulate the middleware context to test get_api_access_token."""
        # Store an MCP access token and its mapping
        await store.set(
            'mcp_access_token',
            'mcp_test_token',
            {
                'token': 'mcp_test_token',
                'client_id': 'client-1',
                'scopes': ['read'],
                'expires_at': 9999999999,
            },
        )
        await store.set_mapping('mcp_access_token', 'mcp_test_token', 'api_access_token', 'api_real_token')

        # Mock the MCP middleware context
        mock_access_token = MagicMock()
        mock_access_token.token = 'mcp_test_token'

        with patch('openapi_mcp_gateway.auth.provider.get_access_token', return_value=mock_access_token):
            result = await provider.get_api_access_token()

        assert result == 'api_real_token'

    async def test_get_api_access_token_no_context(self, provider):
        with patch('openapi_mcp_gateway.auth.provider.get_access_token', return_value=None):
            result = await provider.get_api_access_token()
        assert result is None
