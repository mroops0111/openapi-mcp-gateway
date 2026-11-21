import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.provider import TokenError
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.assembly import AuthTypeContext
from openapi_mcp_gateway.auth.flows import (
    AuthorizationCodeFlowHandler,
    OAuthFlowContext,
)
from openapi_mcp_gateway.auth.flows.factory import resolve_upstream_oauth_flow
from openapi_mcp_gateway.auth.mcp.authorization_code import McpAuthorizationCodeServer
from openapi_mcp_gateway.auth.mcp.token_issuer import McpTokenPolicy
from openapi_mcp_gateway.scopes import (
    McpAccessToken,
    McpUnrestrictedScopePolicy,
    UpstreamFixedScopeRequest,
    UpstreamScopeRequest,
)
from openapi_mcp_gateway.settings import AuthConfig, UpstreamAuthConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import (
    API_URL,
    ATTACKER_URL,
    AUTHORIZE_URL,
    ISSUER,
    TOKEN_URL,
)
from tests.flow import (
    server_entry,
    spec_with_authorization_code,
)
from tests.oauth import (
    BROWSER_ID,
    begin_upstream,
    bridge_of,
    code_in,
    complete_upstream,
    deliver_callback,
    query_value,
    server_of,
    sign_in,
    upstream_token_response,
)


class TestAuthorizationCodeFlowHandler:
    """Direct tests for the AuthorizationCodeFlowHandler."""

    def test_provider_carries_callback_url(self):
        """Handler composes the callback URL from gateway_url + mount_path."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api'])
            ),
        )
        spec = spec_with_authorization_code()
        handler = AuthorizationCodeFlowHandler()
        flow = resolve_upstream_oauth_flow(entry, spec)

        setup = handler.build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=spec,
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000/',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=flow,
            )
        )

        assert setup.provider is not None
        assert bridge_of(setup.provider).upstream.callback_url == 'http://localhost:8000/srv/auth/callback'
        assert bridge_of(setup.provider).upstream.authorization_url == AUTHORIZE_URL
        assert bridge_of(setup.provider).upstream.token_url == TOKEN_URL
        # The provider's issuer is the string the metadata publishes, which RFC 9207 compares exactly.
        assert setup.settings is not None
        assert server_of(setup.provider).issuer == str(setup.settings.issuer_url) == 'http://localhost:8000/srv'
        assert setup.mcp_summary == 'gateway (authorization_code)'

    def test_a_root_mount_joins_the_callback_url_with_one_slash(self):
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api'])
            ),
        ).model_copy(update={'path_prefix': '/'})
        spec = spec_with_authorization_code()

        setup = AuthorizationCodeFlowHandler().build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=spec,
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=resolve_upstream_oauth_flow(entry, spec),
            )
        )

        assert setup.provider is not None
        assert bridge_of(setup.provider).upstream.callback_url == 'http://localhost:8000/auth/callback'


class TestUpstreamAudienceWiring:
    """Config-level ``resource`` / ``audience`` reach the component that talks to the upstream."""

    def test_authorization_code_provider_receives_audience(self):
        """``AuthorizationCodeFlowHandler`` hands the resolved parameters to the provider."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', audience=API_URL)
            ),
        )
        spec = spec_with_authorization_code()

        setup = AuthorizationCodeFlowHandler().build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=spec,
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=resolve_upstream_oauth_flow(entry, spec),
            )
        )

        assert setup.provider is not None
        assert bridge_of(setup.provider).upstream.audience_params == {'audience': API_URL}

    def test_authorization_code_provider_receives_upstream_issuer(self):
        """``upstream.issuer`` reaches the provider, which checks the authorization response's ``iss`` against it."""
        entry = server_entry(
            AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', issuer=ISSUER)),
        )
        spec = spec_with_authorization_code()

        setup = AuthorizationCodeFlowHandler().build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=spec,
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=resolve_upstream_oauth_flow(entry, spec),
            )
        )

        assert setup.provider is not None
        assert bridge_of(setup.provider).upstream.issuer == ISSUER
        assert bridge_of(setup.provider).upstream.authorization_server == ISSUER


class TestAuthorizationCodeScopes:
    """``required_scopes`` reaches the tokens the gateway mints, rather than being ignored."""

    def _build(self, **mcp_overrides):
        auth = {'upstream': {'type': 'oauth2', 'client_id': 'cid', 'client_secret': 'sec'}, 'mcp': mcp_overrides}
        entry = server_entry(AuthConfig.model_validate(auth))
        spec = spec_with_authorization_code()
        return AuthorizationCodeFlowHandler().build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=spec,
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=resolve_upstream_oauth_flow(entry, spec),
            )
        )

    def test_required_scopes_drive_what_the_gateway_issues(self):
        """The gateway is the issuer here, so the scopes it demands are the ones it grants.

        Previously the value was hardcoded, so configuring it changed nothing and said nothing.
        """
        setup = self._build(required_scopes=['reports.read', 'reports.write'])

        assert setup.settings is not None
        assert setup.settings.required_scopes == ['reports.read', 'reports.write']
        registration = setup.settings.client_registration_options
        assert registration is not None
        assert registration.valid_scopes == ['reports.read', 'reports.write']
        assert setup.provider is not None
        assert server_of(setup.provider).tokens.policy.scopes == ['reports.read', 'reports.write']

    def test_a_default_scope_stands_in_when_none_is_configured(self):
        """OAuth needs a scope to name even when a deployment has no opinion about them."""
        setup = self._build()

        assert setup.settings is not None
        assert setup.settings.required_scopes == ['api']


class TestUpstreamGrant:
    """Each token records the scopes the upstream granted the user behind it."""

    @pytest.mark.parametrize(('stated', 'recorded'), [('read', ['read']), (None, ['read', 'write'])])
    async def test_a_sign_in_records_the_upstream_grant(self, provider, mcp_client_info, stated, recorded):
        """An omitted ``scope`` means the upstream granted what the gateway requested."""
        token = await sign_in(provider, mcp_client_info, upstream_scope=stated)
        assert await _upstream_scopes_of(provider, token.access_token) == recorded

    @pytest.mark.parametrize(
        ('stated', 'recorded'), [('read write admin', ['read', 'write', 'admin']), (None, ['read'])]
    )
    async def test_an_upstream_refresh_updates_the_grant_it_states(
        self, provider, mcp_client_info, memory_store, stated, recorded
    ):
        """A refresh that omits ``scope`` leaves the grant as it was."""
        # An upstream token about to expire, so the refresh path actually calls the token endpoint.
        token = await sign_in(provider, mcp_client_info, upstream_scope='read', upstream_expires_in=30)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        response = upstream_token_response('rotated-access-token', scope=stated)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
            rotated = await provider.exchange_refresh_token(mcp_client_info, refresh, [])

        assert await _upstream_scopes_of(provider, rotated.access_token) == recorded


class TestUpstreamScopesPerSignIn:
    """Each sign-in asks the upstream for what the scope request chooses for the MCP scopes it will grant."""

    async def test_the_upstream_is_asked_for_what_the_mcp_grant_needs(self, make_provider, mcp_client_info):
        provider = make_provider(scope_request=_MirroredScopeRequest())
        await provider.register_client(mcp_client_info)
        upstream_url = await begin_upstream(provider, mcp_client_info, scopes=['issues:read'])
        assert query_value(upstream_url, 'scope') == 'upstream:api upstream:issues:read'

    async def test_an_omitted_upstream_scope_records_what_this_sign_in_asked_for(self, make_provider, mcp_client_info):
        provider = make_provider(scope_request=_MirroredScopeRequest())
        token = await sign_in(provider, mcp_client_info, scopes=['issues:read'])
        assert await _upstream_scopes_of(provider, token.access_token) == ['upstream:api', 'upstream:issues:read']


class TestUpstreamTokenLifetime:
    """An MCP access token never outlives the upstream token behind it, so the client refreshes in time."""

    @pytest.mark.parametrize(
        ('stated', 'lifetime'),
        [('1799', 1799), (None, 3600), (1800.0, 1800), (86400, 3600), (0, 3600), ('1e400', 3600)],
    )
    async def test_the_access_token_lasts_no_longer_than_the_upstream_one(
        self, provider, mcp_client_info, stated, lifetime
    ):
        """``expires_in`` is read leniently, since some upstreams send it as a string or leave it null."""
        response = upstream_token_response()
        response.json.return_value['expires_in'] = stated
        upstream_url = await begin_upstream(provider, mcp_client_info)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
            redirect = await deliver_callback(provider, upstream_url)
        auth_code = await provider.load_authorization_code(mcp_client_info, code_in(redirect))
        assert auth_code is not None

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)

        assert abs(token.expires_in - lifetime) <= 1

    async def test_a_refresh_far_from_upstream_expiry_leaves_the_upstream_alone(self, provider, mcp_client_info):
        """The upstream token is renewed only once it is about to expire."""
        token = await sign_in(provider, mcp_client_info)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        post_mock = AsyncMock()
        with patch('httpx.AsyncClient.post', post_mock):
            await provider.exchange_refresh_token(mcp_client_info, refresh, [])

        post_mock.assert_not_awaited()


class TestUpstreamRenewal:
    """An MCP refresh renews the upstream token once less than half an MCP access token's lifetime remains of it."""

    async def _refresh(self, provider, mcp_client_info, upstream_expires_in, post_mock):
        token = await sign_in(provider, mcp_client_info, upstream_expires_in=upstream_expires_in)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None
        with patch('httpx.AsyncClient.post', post_mock):
            return await provider.exchange_refresh_token(mcp_client_info, refresh, [])

    async def test_a_refresh_well_ahead_of_upstream_expiry_gets_a_full_token(self, provider, mcp_client_info):
        """A client refreshing early is not handed the few minutes left, which it would refresh again at once."""
        post_mock = AsyncMock(return_value=upstream_token_response('renewed', expires_in=3600))

        rotated = await self._refresh(provider, mcp_client_info, 900, post_mock)

        post_mock.assert_awaited_once()
        assert rotated.expires_in == 3600

    async def test_a_failed_renewal_keeps_an_upstream_token_still_working(self, provider, mcp_client_info):
        response = MagicMock(status_code=503, text='unavailable')
        rotated = await self._refresh(provider, mcp_client_info, 900, AsyncMock(return_value=response))

        assert 0 < rotated.expires_in <= 900

    async def test_a_failed_renewal_of_an_expired_upstream_token_is_an_oauth_error(self, provider, mcp_client_info):
        """The client hears ``invalid_grant`` and signs in again, rather than a plain-text HTTP error."""
        response = MagicMock(status_code=400, text='revoked')
        token = await sign_in(provider, mcp_client_info, upstream_expires_in=60)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        with (
            # Two minutes on, so the upstream token signed in above has expired.
            patch('time.time', return_value=time.time() + 120),
            patch('httpx.AsyncClient.post', AsyncMock(return_value=response)),
            pytest.raises(TokenError) as raised,
        ):
            await provider.exchange_refresh_token(mcp_client_info, refresh, [])

        assert raised.value.error == 'invalid_grant'

    async def test_an_upstream_token_without_a_lifetime_leaves_the_configured_ttl(self, make_provider, mcp_client_info):
        """An upstream that states no ``expires_in`` issues tokens that do not expire, so nothing caps the MCP token."""
        provider = make_provider(issued_tokens=McpTokenPolicy(access_token_ttl=28800))
        response = upstream_token_response()
        del response.json.return_value['expires_in']
        upstream_url = await begin_upstream(provider, mcp_client_info)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
            redirect = await deliver_callback(provider, upstream_url)
        auth_code = await provider.load_authorization_code(mcp_client_info, code_in(redirect))
        assert auth_code is not None

        token = await provider.exchange_authorization_code(mcp_client_info, auth_code)

        assert token.expires_in == 28800


class TestRevocationRemovesLinks:
    """Revoking a token leaves none of the upstream credentials it linked at rest."""

    async def test_revoking_the_refresh_token_deletes_both_tokens_links(self, provider, mcp_client_info, memory_store):
        token = await sign_in(provider, mcp_client_info)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        await provider.revoke_token(refresh)

        for namespace, key in [('mcp_access_token', token.access_token), ('mcp_refresh_token', token.refresh_token)]:
            for link in ('upstream_access_token', 'upstream_refresh_token'):
                assert await memory_store.get_mapping(namespace, key, link) is None

    async def test_a_redeemed_code_keeps_no_links(self, provider, mcp_client_info, memory_store):
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))
        code = code_in(redirect)
        auth_code = await provider.load_authorization_code(mcp_client_info, code)
        assert auth_code is not None

        await provider.exchange_authorization_code(mcp_client_info, auth_code)

        assert await memory_store.get_mapping('mcp_auth_code', code, 'upstream_access_token') is None


class TestBridgeUpstreamToken:
    """The Bridge resolves the upstream token behind the MCP token of the call being handled."""

    async def test_the_linked_upstream_token_is_returned(self, provider, memory_store):
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
        await memory_store.set_mapping(
            'mcp_access_token', 'mcp_test_token', 'upstream_access_token', 'upstream_real_token'
        )

        mock_access_token = MagicMock()
        mock_access_token.token = 'mcp_test_token'

        with patch(
            'openapi_mcp_gateway.auth.flows.authorization_code.get_access_token', return_value=mock_access_token
        ):
            result = await bridge_of(provider).get_token()

        assert result == 'upstream_real_token'

    async def test_no_token_without_an_authenticated_call(self, provider):
        """Without an active MCP token context, the resolver returns ``None``."""
        with patch('openapi_mcp_gateway.auth.flows.authorization_code.get_access_token', return_value=None):
            result = await bridge_of(provider).get_token()
        assert result is None


class TestUpstreamCallbackErrors:
    """``handle_upstream_callback`` propagates upstream/state failures as ``HTTPException``."""

    async def test_invalid_state_raises(self, provider):
        """An unknown ``state`` value triggers ``400 Invalid state parameter``."""
        with pytest.raises(HTTPException) as exception_info:
            await bridge_of(provider).handle_upstream_callback('any-code', 'never-issued-state', BROWSER_ID)
        assert exception_info.value.status_code == 400
        assert 'expired' in exception_info.value.detail

    async def test_upstream_non_200_raises(self, provider, mcp_client_info):
        """A non-200 upstream token response surfaces as ``400 Upstream token exchange failed``."""
        upstream_url = await begin_upstream(provider, mcp_client_info)

        bad_response = MagicMock()
        bad_response.status_code = 401
        bad_response.text = 'invalid_client'

        with (
            patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=bad_response),
            pytest.raises(HTTPException) as exception_info,
        ):
            await deliver_callback(provider, upstream_url)
        assert exception_info.value.status_code == 400
        assert 'invalid_client' in exception_info.value.detail

    async def test_upstream_missing_access_token_raises(self, provider, mcp_client_info):
        """A 200 response without ``access_token`` surfaces as ``400 Upstream returned no access_token``."""
        upstream_url = await begin_upstream(provider, mcp_client_info)

        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.json.return_value = {'expires_in': 3600}

        with (
            patch('httpx.AsyncClient.post', new_callable=AsyncMock, return_value=empty_response),
            pytest.raises(HTTPException) as exception_info,
        ):
            await deliver_callback(provider, upstream_url)
        assert exception_info.value.status_code == 400
        assert 'access_token' in exception_info.value.detail


class TestUpstreamAuthorizationResponseIssuer:
    """RFC 9207 ``iss`` on the upstream's authorization response, checked before the code is redeemed."""

    async def _callback_outcome(self, provider, upstream_url: str, iss: str | None) -> tuple[str | None, AsyncMock]:
        """Deliver the upstream's redirect, returning the result (``None`` if refused) and the token mock."""
        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            try:
                return await deliver_callback(provider, upstream_url, iss), post_mock
            except HTTPException as exc:
                assert exc.status_code == 400
                return None, post_mock

    async def test_mismatched_iss_is_rejected_before_the_code_is_redeemed(self, make_provider, mcp_client_info):
        """A response naming another issuer never reaches the token endpoint, the mix-up defence."""
        provider = make_provider(upstream_issuer=ISSUER)
        upstream_url = await begin_upstream(provider, mcp_client_info)

        result, post_mock = await self._callback_outcome(provider, upstream_url, ATTACKER_URL)

        assert result is None
        post_mock.assert_not_awaited()

    async def test_iss_is_compared_without_normalisation(self, make_provider, mcp_client_info):
        """A trailing slash is a different issuer, since RFC 9207 forbids normalising before comparison."""
        provider = make_provider(upstream_issuer=ISSUER)
        upstream_url = await begin_upstream(provider, mcp_client_info)

        result, post_mock = await self._callback_outcome(provider, upstream_url, f'{ISSUER}/')

        assert result is None
        post_mock.assert_not_awaited()

    async def test_matching_iss_proceeds(self, make_provider, mcp_client_info):
        """The recorded issuer is accepted and the code is redeemed."""
        provider = make_provider(upstream_issuer=ISSUER)
        upstream_url = await begin_upstream(provider, mcp_client_info)

        result, post_mock = await self._callback_outcome(provider, upstream_url, ISSUER)

        assert result is not None
        post_mock.assert_awaited_once()

    async def test_absent_iss_proceeds(self, make_provider, mcp_client_info):
        """An upstream that sends no ``iss`` still works, since most do not send one yet."""
        provider = make_provider(upstream_issuer=ISSUER)
        upstream_url = await begin_upstream(provider, mcp_client_info)

        result, _ = await self._callback_outcome(provider, upstream_url, None)

        assert result is not None

    async def test_without_a_configured_issuer_a_present_iss_cannot_be_checked(self, provider, mcp_client_info):
        """Behaviour is unchanged for a config that names no upstream issuer."""
        upstream_url = await begin_upstream(provider, mcp_client_info)

        result, _ = await self._callback_outcome(provider, upstream_url, ATTACKER_URL)

        assert result is not None

    async def test_a_response_for_a_previous_upstream_is_rejected(self, make_provider, mcp_client_info):
        """A sign-in started before the upstream changed is not finished against the new one."""
        before = make_provider(upstream_issuer=ISSUER)
        upstream_url = await begin_upstream(before, mcp_client_info)

        after = make_provider(upstream_issuer='https://new-idp.example.com')
        result, post_mock = await self._callback_outcome(after, upstream_url, ISSUER)

        assert result is None
        post_mock.assert_not_awaited()


async def _upstream_scopes_of(provider: McpAuthorizationCodeServer, mcp_access_token: str) -> list[str]:
    """The upstream grant the MCP access token ``mcp_access_token`` records."""
    access_token = await provider.load_access_token(mcp_access_token)
    assert isinstance(access_token, McpAccessToken)
    return access_token.upstream_scopes


class _MirroredScopeRequest(UpstreamScopeRequest):
    """Asks the upstream for one ``upstream:`` scope per MCP scope, so a request shows which grant it came from."""

    def scopes_for(self, mcp_scopes):
        return [f'upstream:{scope}' for scope in mcp_scopes]
