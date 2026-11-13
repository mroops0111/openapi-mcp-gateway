import typing
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.flows.authorization_code import (
    AuthorizationCodeProvider,
    IssuedTokenPolicy,
    UpstreamOAuthClient,
)
from openapi_mcp_gateway.scopes import BrokeredAccessToken
from tests.constants import (
    API_URL,
    ATTACKER_URL,
    AUTHORIZE_URL,
    BROWSER_ORIGIN,
    GATEWAY_ISSUER,
    GATEWAY_URL,
    ISSUER,
    TOKEN_URL,
)
from tests.oauth import (
    CLIENT_REDIRECT_URI,
    authorize,
    code_in,
    complete_upstream,
    sign_in,
    upstream_token_response,
)


# A second server on the same gateway, its own authorization server sharing the store.
OTHER_GATEWAY_ISSUER = f'{GATEWAY_URL}/inventory'


@pytest.fixture
def make_provider(memory_store) -> typing.Callable[..., AuthorizationCodeProvider]:
    """Build a provider on the shared store, naming ``issuer`` as its own in front of the upstream."""

    def make(
        issuer: str = GATEWAY_ISSUER,
        upstream_issuer: str | None = None,
        token_url: str = TOKEN_URL,
        scopes: list[str] | None = None,
        audience_params: dict[str, str] | None = None,
        issued_tokens: IssuedTokenPolicy | None = None,
    ) -> AuthorizationCodeProvider:
        return AuthorizationCodeProvider(
            store=memory_store,
            upstream=UpstreamOAuthClient(
                authorization_url=AUTHORIZE_URL,
                token_url=token_url,
                client_id='gateway-client-id',
                client_secret='gateway-client-secret',
                callback_url=f'{issuer}/auth/callback',
                scopes=scopes or ['read'],
                audience_params=audience_params or {},
                issuer=upstream_issuer,
            ),
            issued_tokens=issued_tokens or IssuedTokenPolicy(),
            issuer=issuer,
            prefix='petstore',
        )

    return make


@pytest.fixture
def provider(make_provider):
    """The petstore server's provider, requesting ``read`` and ``write`` upstream."""
    return make_provider(scopes=['read', 'write'])


@pytest.fixture
def audience_provider(make_provider):
    """Provider configured to name the upstream API the token is minted for."""
    return make_provider(audience_params={'audience': API_URL})


@pytest.fixture
def mcp_client_info():
    """An MCP client as the SDK's registration handler would record it."""
    return OAuthClientInformationFull(
        client_id='mcp-client-123',
        client_secret='mcp-secret',
        redirect_uris=[AnyHttpUrl(CLIENT_REDIRECT_URI)],
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

    async def test_application_type_is_kept(self, provider):
        """The OIDC ``application_type`` a client registers with survives the round trip through the store.

        The SDK's registration request defaults it to ``native`` (SEP-837),
        so a client that sends none is still recorded with one.
        """
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


class TestAuthorize:
    """``authorize`` builds the upstream authorization URL with carried-over state."""

    async def test_authorize_returns_upstream_url(self, provider, mcp_client_info):
        """The returned URL points at upstream and carries ``client_id`` / ``state`` / ``scope``."""
        url = await authorize(provider, mcp_client_info, 'test-state')
        assert url.startswith(f'{AUTHORIZE_URL}?')
        assert 'client_id=gateway-client-id' in url
        assert 'state=test-state' in url
        assert 'scope=read+write' in url


class TestGrantedScopes:
    """The scopes a token carries, whatever the client asked for."""

    @pytest.mark.parametrize(('requested', 'granted'), [(None, 'a b'), (['a'], 'a b'), (['c'], 'a b c')])
    async def test_endpoint_scopes_are_always_granted(self, make_provider, mcp_client_info, requested, granted):
        """A token without them would be refused by every request, since each must hold them all."""
        provider = make_provider(issued_tokens=IssuedTokenPolicy(scopes=['a', 'b']))
        token = await sign_in(provider, mcp_client_info, scopes=requested)
        assert token.scope == granted

    async def test_state_stored_without_scopes_grants_the_endpoint_scopes(
        self, provider, mcp_client_info, memory_store
    ):
        """A sign-in started before an upgrade still completes, as the state it stored lacks the grant."""
        await authorize(provider, mcp_client_info, 'state-old')
        state_data = await memory_store.get('mcp_auth_state', 'state-old')
        assert state_data is not None
        del state_data['scopes']
        await memory_store.set('mcp_auth_state', 'state-old', state_data, ttl=900)

        auth_code = await provider.load_authorization_code(
            mcp_client_info, code_in(await complete_upstream(provider, 'state-old'))
        )
        assert auth_code is not None
        assert auth_code.scopes == ['api']


class TestUpstreamGrant:
    """Each token records the scopes the upstream granted the user behind it."""

    @pytest.mark.parametrize(('stated', 'recorded'), [('read', ['read']), (None, ['read', 'write'])])
    async def test_a_sign_in_records_the_upstream_grant(self, provider, mcp_client_info, stated, recorded):
        """An omitted ``scope`` means the upstream granted what the gateway requested."""
        token = await sign_in(provider, mcp_client_info, upstream_scope=stated)
        access_token = await provider.load_access_token(token.access_token)
        assert isinstance(access_token, BrokeredAccessToken)
        assert access_token.upstream_scopes == recorded

    @pytest.mark.parametrize(
        ('stated', 'recorded'), [('read write admin', ['read', 'write', 'admin']), (None, ['read'])]
    )
    async def test_an_upstream_refresh_updates_the_grant_it_states(
        self, provider, mcp_client_info, memory_store, stated, recorded
    ):
        """A refresh that omits ``scope`` leaves the grant as it was."""
        token = await sign_in(provider, mcp_client_info, upstream_scope='read')
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None
        # Expire the upstream token so the refresh path actually calls the token endpoint.
        await memory_store.delete('api_access_token', 'upstream-access-token')

        response = upstream_token_response('rotated-access-token', scope=stated)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
            rotated = await provider.exchange_refresh_token(mcp_client_info, refresh, [])

        access_token = await provider.load_access_token(rotated.access_token)
        assert isinstance(access_token, BrokeredAccessToken)
        assert access_token.upstream_scopes == recorded

    async def test_a_token_stored_without_the_grant_holds_the_requested_scopes(
        self, provider, mcp_client_info, memory_store
    ):
        """A token minted before an upgrade keeps working, read as an omitted ``scope`` is."""
        token = await sign_in(provider, mcp_client_info, upstream_scope='read')
        data = await memory_store.get('mcp_access_token', token.access_token)
        assert data is not None
        del data['upstream_scopes']
        await memory_store.set('mcp_access_token', token.access_token, data, ttl=3600)

        access_token = await provider.load_access_token(token.access_token)
        assert isinstance(access_token, BrokeredAccessToken)
        assert access_token.upstream_scopes == ['read', 'write']


class TestFullOAuthLifecycle:
    """Run the end-to-end flow of authorize, callback, exchange, load, refresh, and revoke."""

    async def test_full_flow(self, provider, mcp_client_info, memory_store):
        """Walk through every step of the OAuth lifecycle and verify token mappings."""
        upstream_url = await authorize(provider, mcp_client_info, 'state-xyz', scopes=['read', 'write'])
        assert 'state=state-xyz' in upstream_url

        redirect = await complete_upstream(provider, 'state-xyz')
        mcp_auth_code = code_in(redirect)
        assert mcp_auth_code.startswith('mcp_')

        auth_code = await provider.load_authorization_code(mcp_client_info, mcp_auth_code)
        assert auth_code is not None
        assert auth_code.client_id == 'mcp-client-123'

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)
        assert token.access_token.startswith('mcp_')
        assert token.refresh_token is not None
        assert token.refresh_token.startswith('mcp_refresh_')
        assert token.expires_in == 3600

        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.client_id == 'mcp-client-123'

        upstream = await memory_store.get_mapping('mcp_access_token', token.access_token, 'api_access_token')
        assert upstream == 'upstream-access-token'

        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        new_token = await provider.exchange_refresh_token(mcp_client_info, refresh, ['read', 'write'])
        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token

        assert await provider.load_access_token(token.access_token) is None

        new_upstream = await memory_store.get_mapping('mcp_access_token', new_token.access_token, 'api_access_token')
        assert new_upstream == 'upstream-access-token'

        new_access = await provider.load_access_token(new_token.access_token)
        assert new_access is not None
        await provider.revoke_token(new_access)

        assert new_token.refresh_token is not None
        assert await provider.load_access_token(new_token.access_token) is None
        assert await provider.load_refresh_token(mcp_client_info, new_token.refresh_token) is None


class TestConfigurableTokenTtl:
    """Custom access and refresh TTLs shape the MCP tokens the provider mints."""

    async def test_issued_token_uses_custom_access_ttl(self, make_provider, mcp_client_info):
        """A provider built with a custom access TTL mints tokens expiring on that cadence."""
        provider = make_provider(issued_tokens=IssuedTokenPolicy(access_token_ttl=7200, refresh_token_ttl=604800))

        token = await sign_in(provider, mcp_client_info, 'state-ttl')

        assert token.expires_in == 7200


class TestGetApiAccessToken:
    """``get_api_access_token`` resolves the upstream token from the active MCP context."""

    async def test_get_api_access_token(self, provider, memory_store):
        """Given an MCP access token in context, the mapped upstream token is returned."""
        await memory_store.set(
            'mcp_access_token',
            'mcp_test_token',
            {
                'token': 'mcp_test_token',
                'client_id': 'client-1',
                'scopes': ['read'],
                'expires_at': 9999999999,
            },
        )
        await memory_store.set_mapping('mcp_access_token', 'mcp_test_token', 'api_access_token', 'api_real_token')

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
        await authorize(provider, mcp_client_info, 'state-bad-upstream')

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
        await authorize(provider, mcp_client_info, 'state-missing-token')

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


class TestUpstreamAudienceParams:
    """The audience naming the upstream API rides on every upstream request.

    Without it an authorization server mints a token for its own default audience,
    which an API that merely trusts that issuer will refuse.
    """

    async def test_authorize_url_carries_audience(self, audience_provider, mcp_client_info):
        """The browser redirect names the API, since consent binds the audience."""
        url = await authorize(audience_provider, mcp_client_info, 'state-aud')

        assert urllib.parse.urlencode({'audience': API_URL}) in url

    async def test_code_exchange_carries_audience(self, audience_provider, mcp_client_info):
        """The authorization_code grant names the API as well as the authorize request."""
        await authorize(audience_provider, mcp_client_info, 'state-aud')

        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            await audience_provider.handle_upstream_callback('upstream-code', 'state-aud')

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert posted['grant_type'] == 'authorization_code'
        assert posted['audience'] == API_URL

    async def test_refresh_carries_audience(self, audience_provider, mcp_client_info, memory_store):
        """A refresh names the API too, so the rotated token stays usable upstream.

        Dropping the audience here would return a token the upstream refuses,
        and that failure would only surface once the first token expired.
        """
        token = await sign_in(audience_provider, mcp_client_info, 'state-aud')
        assert token.refresh_token is not None
        refresh = await audience_provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        # Expire the upstream token so the refresh path actually calls the token endpoint.
        await memory_store.delete('api_access_token', 'upstream-access-token')

        post_mock = AsyncMock(return_value=upstream_token_response('rotated-access-token'))
        with patch('httpx.AsyncClient.post', post_mock):
            await audience_provider.exchange_refresh_token(mcp_client_info, refresh, ['read'])

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert posted['grant_type'] == 'refresh_token'
        assert posted['audience'] == API_URL

    async def test_unconfigured_provider_sends_no_audience(self, provider, mcp_client_info):
        """An upstream that issues its own tokens sees no audience keys at all."""
        url = await authorize(provider, mcp_client_info, 'state-plain')
        assert 'audience=' not in url
        assert 'resource=' not in url

        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            await provider.handle_upstream_callback('upstream-code', 'state-plain')

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert 'audience' not in posted
        assert 'resource' not in posted


class TestUpstreamAuthorizationResponseIssuer:
    """RFC 9207 ``iss`` on the upstream's authorization response, checked before the code is redeemed."""

    async def _callback_outcome(self, provider, state: str, iss: str | None) -> tuple[str | None, AsyncMock]:
        """Deliver the upstream's redirect, returning the result (``None`` if refused) and the token mock."""
        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            try:
                return await provider.handle_upstream_callback('upstream-code', state, iss), post_mock
            except HTTPException as exc:
                assert exc.status_code == 400
                return None, post_mock

    async def test_mismatched_iss_is_rejected_before_the_code_is_redeemed(self, make_provider, mcp_client_info):
        """A response naming another issuer never reaches the token endpoint, the mix-up defence."""
        provider = make_provider(upstream_issuer=ISSUER)
        await authorize(provider, mcp_client_info, 'state-mixup')

        result, post_mock = await self._callback_outcome(provider, 'state-mixup', ATTACKER_URL)

        assert result is None
        post_mock.assert_not_awaited()

    async def test_iss_is_compared_without_normalisation(self, make_provider, mcp_client_info):
        """A trailing slash is a different issuer, since RFC 9207 forbids normalising before comparison."""
        provider = make_provider(upstream_issuer=ISSUER)
        await authorize(provider, mcp_client_info, 'state-slash')

        result, post_mock = await self._callback_outcome(provider, 'state-slash', f'{ISSUER}/')

        assert result is None
        post_mock.assert_not_awaited()

    async def test_matching_iss_proceeds(self, make_provider, mcp_client_info):
        """The recorded issuer is accepted and the code is redeemed."""
        provider = make_provider(upstream_issuer=ISSUER)
        await authorize(provider, mcp_client_info, 'state-match')

        result, post_mock = await self._callback_outcome(provider, 'state-match', ISSUER)

        assert result is not None
        post_mock.assert_awaited_once()

    async def test_absent_iss_proceeds(self, make_provider, mcp_client_info):
        """An upstream that sends no ``iss`` still works, since most do not send one yet."""
        provider = make_provider(upstream_issuer=ISSUER)
        await authorize(provider, mcp_client_info, 'state-quiet')

        result, _ = await self._callback_outcome(provider, 'state-quiet', None)

        assert result is not None

    async def test_without_a_configured_issuer_a_present_iss_cannot_be_checked(self, provider, mcp_client_info):
        """Behaviour is unchanged for a config that names no upstream issuer."""
        await authorize(provider, mcp_client_info, 'state-unconfigured')

        result, _ = await self._callback_outcome(provider, 'state-unconfigured', ATTACKER_URL)

        assert result is not None

    async def test_a_response_for_a_previous_upstream_is_rejected(self, make_provider, mcp_client_info):
        """A sign-in started before the upstream changed is not finished against the new one."""
        before = make_provider(upstream_issuer=ISSUER)
        await authorize(before, mcp_client_info, 'state-moved')

        after = make_provider(upstream_issuer='https://new-idp.example.com')
        result, post_mock = await self._callback_outcome(after, 'state-moved', ISSUER)

        assert result is None
        post_mock.assert_not_awaited()


class TestGatewayAuthorizationResponseIssuer:
    """The gateway names itself in its own authorization responses (RFC 9207 §2)."""

    async def test_redirect_to_the_mcp_client_carries_iss(self, provider, mcp_client_info):
        """``iss`` equals the issuer the gateway's metadata publishes for this server."""
        await authorize(provider, mcp_client_info, 'state-own-iss')

        redirect = await complete_upstream(provider, 'state-own-iss')

        query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)
        assert query['iss'] == [GATEWAY_ISSUER]
        assert query['state'] == ['state-own-iss']


class TestClientRegistrationBinding:
    """MCP client registrations are keyed by the issuer they were registered with."""

    async def test_a_client_registered_with_one_server_is_unknown_to_another(self, make_provider, mcp_client_info):
        """Two servers share the store, but not each other's clients."""
        petstore = make_provider(issuer=GATEWAY_ISSUER)
        inventory = make_provider(issuer=OTHER_GATEWAY_ISSUER)

        await petstore.register_client(mcp_client_info)

        assert await petstore.get_client('mcp-client-123') is not None
        assert await inventory.get_client('mcp-client-123') is None

    async def test_a_registration_stored_before_binding_degrades_to_re_registration(
        self, provider, mcp_client_info, memory_store
    ):
        """A legacy record keyed by bare ``client_id`` reads as unknown rather than failing.

        The client registers again, and the new record is the one found from then on.
        """
        await memory_store.set(
            'mcp_client', 'mcp-client-123', mcp_client_info.model_dump(exclude_none=True, mode='json')
        )

        assert await provider.get_client('mcp-client-123') is None

        await provider.register_client(mcp_client_info)
        assert await provider.get_client('mcp-client-123') is not None


class TestTokenBinding:
    """MCP codes and tokens are honoured only by the server, and against the upstream, that issued them."""

    async def test_a_token_minted_by_one_server_is_refused_by_another(self, make_provider, mcp_client_info):
        """Otherwise one server's token would unlock the upstream credential mapped behind it on another."""
        petstore = make_provider(issuer=GATEWAY_ISSUER)
        inventory = make_provider(issuer=OTHER_GATEWAY_ISSUER)
        token = await sign_in(petstore, mcp_client_info)

        assert await petstore.load_access_token(token.access_token) is not None
        assert await inventory.load_access_token(token.access_token) is None
        await inventory.register_client(mcp_client_info)
        assert token.refresh_token is not None
        assert await inventory.load_refresh_token(mcp_client_info, token.refresh_token) is None

    async def test_a_changed_upstream_forces_re_authorization(self, make_provider, mcp_client_info):
        """Tokens tied to the previous upstream are refused, so its refresh token is never sent to the new one."""
        before = make_provider(token_url='https://old-idp.example.com/token')
        token = await sign_in(before, mcp_client_info)

        after = make_provider(token_url='https://new-idp.example.com/token')
        assert token.refresh_token is not None
        assert await after.load_access_token(token.access_token) is None
        assert await after.load_refresh_token(mcp_client_info, token.refresh_token) is None

    async def test_the_upstream_issuer_identifies_the_upstream_when_configured(self, make_provider, mcp_client_info):
        """With an issuer configured, moving the token endpoint alone is not a different authorization server."""
        before = make_provider(upstream_issuer=ISSUER)
        token = await sign_in(before, mcp_client_info)

        after = make_provider(upstream_issuer=ISSUER, token_url=f'{ISSUER}/oauth/token')
        assert await after.load_access_token(token.access_token) is not None

    async def test_tokens_stored_before_binding_are_refused(self, provider, memory_store):
        """A legacy record carries no binding, so the client refreshes or signs in again."""
        await memory_store.set(
            'mcp_access_token',
            'mcp_legacy',
            {'token': 'mcp_legacy', 'client_id': 'mcp-client-123', 'scopes': ['api'], 'expires_at': 9999999999},
        )

        assert await provider.load_access_token('mcp_legacy') is None

    async def test_code_exchange_uses_only_the_codes_own_upstream_token(self, provider, mcp_client_info, memory_store):
        """A code with no upstream token of its own is refused, rather than borrowing the client's latest token.

        The client-wide fallback this replaced could hand one user's upstream token to another user,
        when both sign in through the same ``client_id``.
        """
        await authorize(provider, mcp_client_info, 'state-lost')
        record = await memory_store.get('mcp_auth_code', code_in(await complete_upstream(provider, 'state-lost')))
        # The same code under a new key carries no upstream token mapping, as if that mapping had expired.
        await memory_store.set('mcp_auth_code', 'mcp_orphan', {**record, 'code': 'mcp_orphan'})
        await memory_store.set_mapping('client', 'mcp-client-123', 'api_access_token', 'someone-elses-token')
        auth_code = await provider.load_authorization_code(mcp_client_info, 'mcp_orphan')
        assert auth_code is not None

        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(mcp_client_info, auth_code)
