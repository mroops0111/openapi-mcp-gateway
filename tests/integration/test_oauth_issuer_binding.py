from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.flows.authorization_code import (
    AuthorizationCodeProvider,
    IssuedTokenPolicy,
    UpstreamOAuthClient,
)
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.constants import ATTACKER_URL, AUTHORIZE_URL, BROWSER_ORIGIN, GATEWAY_ISSUER, GATEWAY_URL, ISSUER, TOKEN_URL


# A second server on the same gateway, its own authorization server sharing the store.
OTHER_GATEWAY_ISSUER = f'{GATEWAY_URL}/inventory'


@pytest.fixture
def store():
    """One store shared by every provider in a test, as the gateway shares one across servers."""
    return MemoryTokenStore()


@pytest.fixture
def client_info():
    """An MCP client as the SDK's registration handler would record it."""
    return OAuthClientInformationFull(
        client_id='mcp-client-123',
        client_secret='mcp-secret',
        redirect_uris=[AnyHttpUrl('http://localhost:3000/callback')],
    )


def _provider(
    store,
    issuer: str = GATEWAY_ISSUER,
    upstream_issuer: str | None = None,
    token_url: str = TOKEN_URL,
) -> AuthorizationCodeProvider:
    """A provider naming ``issuer`` as its own, in front of an upstream at ``token_url``."""
    return AuthorizationCodeProvider(
        store=store,
        upstream=UpstreamOAuthClient(
            authorization_url=AUTHORIZE_URL,
            token_url=token_url,
            client_id='gateway-client-id',
            client_secret='gateway-client-secret',
            callback_url=f'{issuer}/auth/callback',
            scopes=['read'],
            issuer=upstream_issuer,
        ),
        issued_tokens=IssuedTokenPolicy(),
        issuer=issuer,
    )


def _token_response() -> MagicMock:
    """Mock upstream token endpoint response carrying an access and a refresh token."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'access_token': 'upstream-access-token',
        'refresh_token': 'upstream-refresh-token',
        'expires_in': 3600,
    }
    return response


async def _authorize(provider, client_info, state: str) -> None:
    """Register ``client_info`` and record ``state``, as the start of a sign-in."""
    await provider.register_client(client_info)
    params = AuthorizationParams(
        state=state,
        scopes=['read'],
        redirect_uri=AnyHttpUrl('http://localhost:3000/callback'),
        redirect_uri_provided_explicitly=True,
        code_challenge='challenge-abc',
    )
    await provider.authorize(client_info, params)


async def _sign_in(provider, client_info, state: str = 'state-sign-in'):
    """Run a whole authorization through ``provider`` and return the MCP tokens it issues."""
    await _authorize(provider, client_info, state)
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=_token_response())):
        redirect = await provider.handle_upstream_callback('upstream-code', state)
    mcp_auth_code = parse_qs(urlparse(redirect).query)['code'][0]
    auth_code = await provider.load_authorization_code(client_info, mcp_auth_code)
    assert auth_code is not None
    return await provider.exchange_authorization_code(client_info, auth_code)


class TestUpstreamAuthorizationResponseIssuer:
    """RFC 9207 ``iss`` on the upstream's authorization response, checked before the code is redeemed."""

    async def _callback(self, provider, state: str, iss: str | None) -> tuple[str | None, AsyncMock]:
        """Deliver the upstream's redirect, returning the result (``None`` if refused) and the token mock."""
        post_mock = AsyncMock(return_value=_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            try:
                return await provider.handle_upstream_callback('upstream-code', state, iss), post_mock
            except HTTPException as exc:
                assert exc.status_code == 400
                return None, post_mock

    async def test_mismatched_iss_is_rejected_before_the_code_is_redeemed(self, store, client_info):
        """A response naming another issuer never reaches the token endpoint, the mix-up defence."""
        provider = _provider(store, upstream_issuer=ISSUER)
        await _authorize(provider, client_info, 'state-mixup')

        result, post_mock = await self._callback(provider, 'state-mixup', ATTACKER_URL)

        assert result is None
        post_mock.assert_not_awaited()

    async def test_iss_is_compared_without_normalisation(self, store, client_info):
        """A trailing slash is a different issuer, since RFC 9207 forbids normalising before comparison."""
        provider = _provider(store, upstream_issuer=ISSUER)
        await _authorize(provider, client_info, 'state-slash')

        result, post_mock = await self._callback(provider, 'state-slash', f'{ISSUER}/')

        assert result is None
        post_mock.assert_not_awaited()

    async def test_matching_iss_proceeds(self, store, client_info):
        """The recorded issuer is accepted and the code is redeemed."""
        provider = _provider(store, upstream_issuer=ISSUER)
        await _authorize(provider, client_info, 'state-match')

        result, post_mock = await self._callback(provider, 'state-match', ISSUER)

        assert result is not None
        post_mock.assert_awaited_once()

    async def test_absent_iss_proceeds(self, store, client_info):
        """An upstream that sends no ``iss`` still works, since most do not send one yet."""
        provider = _provider(store, upstream_issuer=ISSUER)
        await _authorize(provider, client_info, 'state-quiet')

        result, _ = await self._callback(provider, 'state-quiet', None)

        assert result is not None

    async def test_without_a_configured_issuer_a_present_iss_cannot_be_checked(self, store, client_info):
        """Behaviour is unchanged for a config that names no upstream issuer."""
        provider = _provider(store)
        await _authorize(provider, client_info, 'state-unconfigured')

        result, _ = await self._callback(provider, 'state-unconfigured', ATTACKER_URL)

        assert result is not None

    async def test_a_response_for_a_previous_upstream_is_rejected(self, store, client_info):
        """A sign-in started before the upstream changed is not finished against the new one."""
        before = _provider(store, upstream_issuer=ISSUER)
        await _authorize(before, client_info, 'state-moved')

        after = _provider(store, upstream_issuer='https://new-idp.example.com')
        result, post_mock = await self._callback(after, 'state-moved', ISSUER)

        assert result is None
        post_mock.assert_not_awaited()


class TestGatewayAuthorizationResponseIssuer:
    """The gateway names itself in its own authorization responses (RFC 9207 §2)."""

    async def test_redirect_to_the_mcp_client_carries_iss(self, store, client_info):
        """``iss`` equals the issuer the gateway's metadata publishes for this server."""
        provider = _provider(store)
        await _authorize(provider, client_info, 'state-own-iss')
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=_token_response())):
            redirect = await provider.handle_upstream_callback('upstream-code', 'state-own-iss')

        query = parse_qs(urlparse(redirect).query)
        assert query['iss'] == [GATEWAY_ISSUER]
        assert query['state'] == ['state-own-iss']


class TestClientRegistrationBinding:
    """MCP client registrations are keyed by the issuer they were registered with."""

    async def test_a_client_registered_with_one_server_is_unknown_to_another(self, store, client_info):
        """Two servers share the store, but not each other's clients."""
        petstore = _provider(store, issuer=GATEWAY_ISSUER)
        inventory = _provider(store, issuer=OTHER_GATEWAY_ISSUER)

        await petstore.register_client(client_info)

        assert await petstore.get_client('mcp-client-123') is not None
        assert await inventory.get_client('mcp-client-123') is None

    async def test_a_registration_stored_before_binding_degrades_to_re_registration(self, store, client_info):
        """A legacy record keyed by bare ``client_id`` reads as unknown rather than failing.

        The client registers again, and the new record is the one found from then on.
        """
        await store.set('mcp_client', 'mcp-client-123', client_info.model_dump(exclude_none=True, mode='json'))
        provider = _provider(store)

        assert await provider.get_client('mcp-client-123') is None

        await provider.register_client(client_info)
        assert await provider.get_client('mcp-client-123') is not None

    async def test_application_type_is_kept(self, store):
        """The OIDC ``application_type`` a client registers with survives the round trip through the store.

        The SDK's registration request defaults it to ``native`` (SEP-837),
        so a client that sends none is still recorded with one.
        """
        provider = _provider(store)
        for client_id, application_type in (('web-client', 'web'), ('native-client', None)):
            fields: dict[str, object] = {'redirect_uris': [f'{BROWSER_ORIGIN}/callback']}
            if application_type:
                fields['application_type'] = application_type
            metadata = OAuthClientMetadata.model_validate(fields)
            await provider.register_client(
                OAuthClientInformationFull(client_id=client_id, **metadata.model_dump(exclude_none=True))
            )

        web = await provider.get_client('web-client')
        native = await provider.get_client('native-client')
        assert web is not None and web.application_type == 'web'
        assert native is not None and native.application_type == 'native'


class TestTokenBinding:
    """MCP codes and tokens are honoured only by the server, and against the upstream, that issued them."""

    async def test_a_token_minted_by_one_server_is_refused_by_another(self, store, client_info):
        """Otherwise one server's token would unlock the upstream credential mapped behind it on another."""
        petstore = _provider(store, issuer=GATEWAY_ISSUER)
        inventory = _provider(store, issuer=OTHER_GATEWAY_ISSUER)
        token = await _sign_in(petstore, client_info)

        assert await petstore.load_access_token(token.access_token) is not None
        assert await inventory.load_access_token(token.access_token) is None
        await inventory.register_client(client_info)
        assert token.refresh_token is not None
        assert await inventory.load_refresh_token(client_info, token.refresh_token) is None

    async def test_a_changed_upstream_forces_re_authorization(self, store, client_info):
        """Tokens tied to the previous upstream are refused, so its refresh token is never sent to the new one."""
        before = _provider(store, token_url='https://old-idp.example.com/token')
        token = await _sign_in(before, client_info)

        after = _provider(store, token_url='https://new-idp.example.com/token')
        assert token.refresh_token is not None
        assert await after.load_access_token(token.access_token) is None
        assert await after.load_refresh_token(client_info, token.refresh_token) is None

    async def test_the_upstream_issuer_identifies_the_upstream_when_configured(self, store, client_info):
        """With an issuer configured, moving the token endpoint alone is not a different authorization server."""
        before = _provider(store, upstream_issuer=ISSUER, token_url=TOKEN_URL)
        token = await _sign_in(before, client_info)

        after = _provider(store, upstream_issuer=ISSUER, token_url=f'{ISSUER}/oauth/token')
        assert await after.load_access_token(token.access_token) is not None

    async def test_tokens_stored_before_binding_are_refused(self, store):
        """A legacy record carries no binding, so the client refreshes or signs in again."""
        await store.set(
            'mcp_access_token',
            'mcp_legacy',
            {'token': 'mcp_legacy', 'client_id': 'mcp-client-123', 'scopes': ['api'], 'expires_at': 9999999999},
        )
        provider = _provider(store)

        assert await provider.load_access_token('mcp_legacy') is None

    async def test_code_exchange_uses_only_the_codes_own_upstream_token(self, store, client_info):
        """A code whose upstream mapping is gone is refused, rather than borrowing the client's latest token.

        The client-wide fallback this replaced could hand one user's upstream token to another user
        signing in through the same ``client_id``.
        """
        provider = _provider(store)
        await _authorize(provider, client_info, 'state-lost')
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=_token_response())):
            redirect = await provider.handle_upstream_callback('upstream-code', 'state-lost')
        mcp_auth_code = parse_qs(urlparse(redirect).query)['code'][0]
        auth_code = await provider.load_authorization_code(client_info, mcp_auth_code)
        assert auth_code is not None

        await store.delete('mcp_auth_code__to__api_access_token', mcp_auth_code)
        await store.set_mapping('client', 'mcp-client-123', 'api_access_token', 'someone-elses-token')

        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(client_info, auth_code)
