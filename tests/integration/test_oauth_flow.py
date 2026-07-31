from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.flows.authorization_code import AuthorizationCodeProvider
from openapi_mcp_gateway.stores.memory import MemoryTokenStore


@pytest.fixture
def store():
    """Fresh in-memory token store for each test."""
    return MemoryTokenStore()


@pytest.fixture
def provider(store):
    """``AuthorizationCodeProvider`` wired against ``auth.example.com`` for the petstore prefix."""
    return AuthorizationCodeProvider(
        store=store,
        upstream_auth_url='https://auth.example.com/authorize',
        upstream_token_url='https://auth.example.com/token',
        client_id='gateway-client-id',
        client_secret='gateway-client-secret',
        callback_url='http://localhost:8000/petstore/auth/callback',
        scopes=['read', 'write'],
        prefix='petstore',
        mcp_access_token_ttl=3600,
        mcp_refresh_token_ttl=86400,
    )


@pytest.fixture
def mcp_client_info():
    """Minimal MCP-side client registration used as the requesting party."""
    return OAuthClientInformationFull(
        client_id='mcp-client-123',
        client_secret='mcp-secret',
        redirect_uris=[AnyHttpUrl('http://localhost:3000/callback')],
    )


class TestClientRegistration:
    """Registering and looking up MCP-side OAuth clients."""

    async def test_register_and_get(self, provider, mcp_client_info):
        """A registered client can be retrieved by its ``client_id``."""
        await provider.register_client(mcp_client_info)
        retrieved = await provider.get_client('mcp-client-123')
        assert retrieved is not None
        assert retrieved.client_id == 'mcp-client-123'

    async def test_get_nonexistent_client(self, provider):
        """Looking up an unregistered client returns ``None``."""
        result = await provider.get_client('nonexistent')
        assert result is None

    async def test_register_without_client_id_raises(self):
        """A client with no ``client_id`` is rejected.

        mcp v2 enforces this at ``OAuthClientInformationFull`` construction,
        where a ``ValidationError`` (itself a ``ValueError``) is raised.
        That is one layer earlier than the provider's own registration check in earlier SDKs,
        so an invalid client can no longer be built.
        """
        with pytest.raises(ValueError, match='client_id'):
            OAuthClientInformationFull(
                client_id='',
                redirect_uris=[AnyHttpUrl('http://localhost/cb')],
            )


class TestAuthorize:
    """``authorize`` builds the upstream authorization URL with carried-over state."""

    async def test_authorize_returns_upstream_url(self, provider, mcp_client_info):
        """The returned URL points at upstream and carries ``client_id`` / ``state`` / ``scope``."""
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
    """End-to-end flow: authorize → callback → exchange → load → refresh → revoke."""

    async def test_full_flow(self, provider, mcp_client_info, store):
        """Walk through every step of the OAuth lifecycle and verify token mappings."""
        await provider.register_client(mcp_client_info)

        params = AuthorizationParams(
            state='state-xyz',
            scopes=['read', 'write'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-123',
        )
        upstream_url = await provider.authorize(mcp_client_info, params)
        assert 'state=state-xyz' in upstream_url

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
        mcp_auth_code = redirect.split('code=')[1].split('&')[0]

        auth_code = await provider.load_authorization_code(mcp_client_info, mcp_auth_code)
        assert auth_code is not None
        assert auth_code.client_id == 'mcp-client-123'

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)
        assert token.access_token.startswith('mcp_')
        assert token.refresh_token.startswith('mcp_refresh_')
        assert token.expires_in == 3600

        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.client_id == 'mcp-client-123'

        upstream = await store.get_mapping('mcp_access_token', token.access_token, 'api_access_token')
        assert upstream == 'upstream-access-token'

        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        new_token = await provider.exchange_refresh_token(mcp_client_info, refresh, ['read', 'write'])
        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token

        assert await provider.load_access_token(token.access_token) is None

        new_upstream = await store.get_mapping('mcp_access_token', new_token.access_token, 'api_access_token')
        assert new_upstream == 'upstream-access-token'

        new_access = await provider.load_access_token(new_token.access_token)
        await provider.revoke_token(new_access)

        assert await provider.load_access_token(new_token.access_token) is None
        assert await provider.load_refresh_token(mcp_client_info, new_token.refresh_token) is None


class TestConfigurableTokenTtl:
    """Custom access and refresh TTLs shape the MCP tokens the provider mints."""

    async def test_issued_token_uses_custom_access_ttl(self, store, mcp_client_info):
        """A provider built with a custom access TTL mints tokens expiring on that cadence."""
        provider = AuthorizationCodeProvider(
            store=store,
            upstream_auth_url='https://auth.example.com/authorize',
            upstream_token_url='https://auth.example.com/token',
            client_id='gateway-client-id',
            client_secret='gateway-client-secret',
            callback_url='http://localhost:8000/petstore/auth/callback',
            scopes=['read'],
            prefix='petstore',
            mcp_access_token_ttl=7200,
            mcp_refresh_token_ttl=604800,
        )
        await provider.register_client(mcp_client_info)

        params = AuthorizationParams(
            state='state-ttl',
            scopes=['read'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-123',
        )
        await provider.authorize(mcp_client_info, params)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'access_token': 'upstream-access-token',
            'refresh_token': 'upstream-refresh-token',
            'expires_in': 3600,
        }
        with patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=mock_response):
            redirect = await provider.handle_upstream_callback('upstream-code', 'state-ttl')

        mcp_auth_code = redirect.split('code=')[1].split('&')[0]
        auth_code = await provider.load_authorization_code(mcp_client_info, mcp_auth_code)
        assert auth_code is not None

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)
        assert token.expires_in == 7200


class TestGetApiAccessToken:
    """``get_api_access_token`` resolves the upstream token from the active MCP context."""

    async def test_get_api_access_token(self, provider, store):
        """Given an MCP access token in context, the mapped upstream token is returned."""
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

        mock_access_token = MagicMock()
        mock_access_token.token = 'mcp_test_token'

        with patch(
            'openapi_mcp_gateway.auth.flows.authorization_code.get_access_token', return_value=mock_access_token
        ):
            result = await provider.get_api_access_token()

        assert result == 'api_real_token'

    async def test_get_api_access_token_no_context(self, provider):
        """Without an active MCP token context, the resolver returns ``None``."""
        with patch('openapi_mcp_gateway.auth.flows.authorization_code.get_access_token', return_value=None):
            result = await provider.get_api_access_token()
        assert result is None


class TestUpstreamCallbackErrors:
    """``handle_upstream_callback`` propagates upstream/state failures as ``HTTPException``."""

    async def test_invalid_state_raises(self, provider):
        """An unknown ``state`` value triggers ``400 Invalid state parameter``."""
        with pytest.raises(HTTPException) as exception_info:
            await provider.handle_upstream_callback('any-code', 'never-issued-state')
        assert exception_info.value.status_code == 400
        assert 'state' in exception_info.value.detail.lower()

    async def test_upstream_non_200_raises(self, provider, mcp_client_info):
        """A non-200 upstream token response surfaces as ``400 Upstream token exchange failed``."""
        await provider.register_client(mcp_client_info)
        params = AuthorizationParams(
            state='state-bad-upstream',
            scopes=['read'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-abc',
        )
        await provider.authorize(mcp_client_info, params)

        bad_response = MagicMock()
        bad_response.status_code = 401
        bad_response.text = 'invalid_client'

        with (
            patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=bad_response),
            pytest.raises(HTTPException) as exception_info,
        ):
            await provider.handle_upstream_callback('upstream-code', 'state-bad-upstream')
        assert exception_info.value.status_code == 400
        assert 'invalid_client' in exception_info.value.detail

    async def test_upstream_missing_access_token_raises(self, provider, mcp_client_info):
        """A 200 response without ``access_token`` surfaces as ``400 Upstream returned no access_token``."""
        await provider.register_client(mcp_client_info)
        params = AuthorizationParams(
            state='state-missing-token',
            scopes=['read'],
            redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
            redirect_uri_provided_explicitly=True,
            code_challenge='challenge-abc',
        )
        await provider.authorize(mcp_client_info, params)

        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.json.return_value = {'expires_in': 3600}

        with (
            patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=empty_response),
            pytest.raises(HTTPException) as exception_info,
        ):
            await provider.handle_upstream_callback('upstream-code', 'state-missing-token')
        assert exception_info.value.status_code == 400
        assert 'access_token' in exception_info.value.detail
