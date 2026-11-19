import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyHttpUrl

from openapi_mcp_gateway.auth.mcp.client import McpPreRegisteredClient, McpPreRegisteredClientDirectory
from openapi_mcp_gateway.auth.mcp.token_issuer import McpTokenPolicy
from openapi_mcp_gateway.scopes import McpAccessToken
from tests.constants import (
    ATTACKER_URL,
    AUTHORIZE_URL,
    BROWSER_ORIGIN,
    GATEWAY_ISSUER,
    GATEWAY_RESOURCE,
    GATEWAY_URL,
    ISSUER,
)
from tests.oauth import (
    BROWSER_ID,
    CLIENT_REDIRECT_URI,
    CLIENT_STATE,
    authorization_code_server,
    authorize,
    begin_upstream,
    bridge_of,
    code_in,
    complete_upstream,
    query_value,
    request_id_in,
    sign_in,
    state_in,
)


# A second server on the same gateway, its own authorization server sharing the store.
OTHER_GATEWAY_ISSUER = f'{GATEWAY_URL}/inventory'


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
    """``authorize`` sends the browser to the consent page, which sends it on to the upstream."""

    async def test_authorize_sends_the_browser_to_the_consent_page(self, provider, mcp_client_info):
        """``authorize`` has no request to read a cookie from, so the consent page decides what happens next."""
        url = await authorize(provider, mcp_client_info)
        assert url.startswith(f'{GATEWAY_ISSUER}/auth/consent?request=')

    async def test_a_root_issuer_joins_the_consent_url_with_one_slash(self, make_provider, mcp_client_info):
        provider = make_provider(issuer=f'{GATEWAY_URL}/')
        await provider.register_client(mcp_client_info)

        url = await authorize(provider, mcp_client_info)
        assert url.startswith(f'{GATEWAY_URL}/auth/consent?request=')

    async def test_the_upstream_url_carries_the_gateway_client_and_scopes(self, provider, mcp_client_info):
        url = await begin_upstream(provider, mcp_client_info)
        assert url.startswith(f'{AUTHORIZE_URL}?')
        assert query_value(url, 'client_id') == 'gateway-client-id'
        assert query_value(url, 'scope') == 'read write'

    async def test_the_upstream_gets_a_fresh_state_rather_than_the_clients(self, provider, mcp_client_info):
        """The client's ``state`` is chosen by whoever crafted the link, so it cannot tie the sign-in to a browser."""
        url = await begin_upstream(provider, mcp_client_info)
        assert state_in(url) != CLIENT_STATE


class TestGrantedScopes:
    """The scopes a token carries, whatever the client asked for."""

    @pytest.mark.parametrize(
        ('requested', 'granted'), [(None, 'api mcp'), (['api'], 'api mcp'), (['issues:read'], 'api mcp issues:read')]
    )
    async def test_endpoint_scopes_are_always_granted(self, make_provider, mcp_client_info, requested, granted):
        """A token without them would be refused by every request, since each must hold them all."""
        provider = make_provider(issued_tokens=McpTokenPolicy(scopes=['api', 'mcp']))
        token = await sign_in(provider, mcp_client_info, scopes=requested)
        assert token.scope == granted

    @pytest.mark.parametrize(
        ('registered', 'grantable'),
        [('api issues:read issues:write', ['api', 'issues:read', 'issues:write']), (None, ['api'])],
    )
    async def test_a_token_records_every_scope_its_client_may_hold(self, provider, registered, grantable):
        """What the token can step up to. A client registered without a scope is held to what it was granted."""
        client = OAuthClientInformationFull(
            client_id='mcp-client-123', redirect_uris=[AnyHttpUrl(CLIENT_REDIRECT_URI)], scope=registered
        )
        token = await sign_in(provider, client)
        access_token = await provider.load_access_token(token.access_token)
        assert isinstance(access_token, McpAccessToken)
        assert access_token.mcp_grantable_scopes == grantable


class TestFullOAuthLifecycle:
    """Run the end-to-end flow of authorize, callback, exchange, load, refresh, and revoke."""

    async def test_full_flow(self, provider, mcp_client_info, memory_store):
        """Walk through every step of the OAuth lifecycle and verify token mappings."""
        await provider.register_client(mcp_client_info)
        upstream_url = await begin_upstream(provider, mcp_client_info, scopes=['read', 'write'])

        redirect = await complete_upstream(provider, upstream_url)
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

        upstream = await memory_store.get_mapping('mcp_access_token', token.access_token, 'upstream_access_token')
        assert upstream == 'upstream-access-token'

        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        new_token = await provider.exchange_refresh_token(mcp_client_info, refresh, ['read', 'write'])
        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token

        assert await provider.load_access_token(token.access_token) is None

        new_upstream = await memory_store.get_mapping(
            'mcp_access_token', new_token.access_token, 'upstream_access_token'
        )
        assert new_upstream == 'upstream-access-token'

        new_access = await provider.load_access_token(new_token.access_token)
        assert new_access is not None
        await provider.revoke_token(new_access)

        assert new_token.refresh_token is not None
        assert await provider.load_access_token(new_token.access_token) is None
        assert await provider.load_refresh_token(mcp_client_info, new_token.refresh_token) is None


class TestAuthorizationCodeReuse:
    """An MCP authorization code is good for one exchange, as OAuth 2.1 requires."""

    async def test_a_redeemed_code_cannot_be_exchanged_again(self, provider, mcp_client_info):
        """A second exchange would mint a second token family that revoking the first leaves working."""
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))
        auth_code = await provider.load_authorization_code(mcp_client_info, code_in(redirect))
        assert auth_code is not None
        await provider.exchange_authorization_code(mcp_client_info, auth_code)

        assert await provider.load_authorization_code(mcp_client_info, auth_code.code) is None
        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(mcp_client_info, auth_code)


class TestConcurrentRedemption:
    """Two exchanges racing for one code never both succeed."""

    async def test_only_one_of_two_concurrent_exchanges_succeeds(self, provider, mcp_client_info):
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))
        auth_code = await provider.load_authorization_code(mcp_client_info, code_in(redirect))
        assert auth_code is not None

        outcomes = await asyncio.gather(
            provider.exchange_authorization_code(mcp_client_info, auth_code),
            provider.exchange_authorization_code(mcp_client_info, auth_code),
            return_exceptions=True,
        )

        assert sum(isinstance(outcome, TokenError) for outcome in outcomes) == 1


class TestConsentHandOver:
    """The sign-in survives a failure in the step after consent, so the consent page can retry it."""

    async def test_a_failed_hand_over_keeps_the_sign_in(self, provider, mcp_client_info):
        consent_url = await authorize(provider, mcp_client_info)
        pending = await provider.claim_authorization(request_id_in(consent_url), BROWSER_ID)
        assert pending is not None

        failing = AsyncMock(side_effect=RuntimeError('store unavailable'))
        with patch.object(bridge_of(provider), 'continue_after_consent', failing), pytest.raises(RuntimeError):
            await provider.continue_after_consent(pending)

        assert await provider.load_shown_authorization(pending.request_id, BROWSER_ID) is not None


class TestConfigurableTokenTtl:
    """Custom access and refresh TTLs shape the MCP tokens the provider mints."""

    async def test_issued_token_uses_custom_access_ttl(self, make_provider, mcp_client_info):
        """A provider built with a custom access TTL mints tokens expiring on that cadence."""
        provider = make_provider(issued_tokens=McpTokenPolicy(access_token_ttl=7200, refresh_token_ttl=604800))

        token = await sign_in(provider, mcp_client_info, upstream_expires_in=86400)

        assert token.expires_in == 7200


class TestGatewayAuthorizationResponseIssuer:
    """The gateway names itself in its own authorization responses (RFC 9207 §2)."""

    async def test_redirect_to_the_mcp_client_carries_iss(self, provider, mcp_client_info):
        """``iss`` equals the issuer the gateway's metadata publishes for this server."""
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))

        assert query_value(redirect, 'iss') == GATEWAY_ISSUER

    async def test_redirect_to_the_mcp_client_carries_its_own_state(self, provider, mcp_client_info):
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))
        assert query_value(redirect, 'state') == CLIENT_STATE


class TestClientRegistrationIssuer:
    """MCP client registrations are keyed by the issuer they were registered with."""

    async def test_a_client_registered_with_one_server_is_unknown_to_another(self, make_provider, mcp_client_info):
        """Two servers share the store, but not each other's clients."""
        petstore = make_provider(issuer=GATEWAY_ISSUER)
        inventory = make_provider(issuer=OTHER_GATEWAY_ISSUER)

        await petstore.register_client(mcp_client_info)

        assert await petstore.get_client('mcp-client-123') is not None
        assert await inventory.get_client('mcp-client-123') is None

    async def test_a_registration_stored_before_issuer_keys_degrades_to_re_registration(
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


class TestClientRemoval:
    """Removing a client from the config revokes the tokens it already holds, even ones a shared store kept."""

    async def test_a_token_of_a_removed_client_is_refused(self, make_provider, memory_store):
        upstream = bridge_of(make_provider()).upstream
        agent = McpPreRegisteredClient(
            client_id='agent', redirect_uris=[AnyHttpUrl(CLIENT_REDIRECT_URI)], scope='api', default_scopes=['api']
        )
        before = authorization_code_server(
            memory_store, GATEWAY_ISSUER, upstream, clients=McpPreRegisteredClientDirectory([agent])
        )
        token = await sign_in(before, agent)

        after = authorization_code_server(
            memory_store, GATEWAY_ISSUER, upstream, clients=McpPreRegisteredClientDirectory([])
        )

        assert await before.load_access_token(token.access_token) is not None
        assert await after.load_access_token(token.access_token) is None


class TestTokenResource:
    """Every token names this server's MCP endpoint as its RFC 8707 resource, which the SDK checks on each request."""

    async def test_tokens_are_issued_for_the_endpoint(self, make_provider, mcp_client_info):
        provider = make_provider()
        token = await sign_in(provider, mcp_client_info)

        access_token = await provider.load_access_token(token.access_token)
        assert access_token is not None
        assert access_token.resource == GATEWAY_RESOURCE
        assert token.refresh_token is not None
        refresh_token = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh_token is not None
        assert refresh_token.resource == GATEWAY_RESOURCE

    async def test_a_sign_in_for_another_resource_is_refused(self, make_provider, mcp_client_info):
        provider = make_provider()
        await provider.register_client(mcp_client_info)

        redirect = await authorize(provider, mcp_client_info, resource=f'{ATTACKER_URL}/mcp')

        assert redirect.startswith(CLIENT_REDIRECT_URI)
        assert query_value(redirect, 'error') == 'invalid_target'
        assert query_value(redirect, 'state') == CLIENT_STATE
        assert query_value(redirect, 'iss') == GATEWAY_ISSUER

    async def test_a_sign_in_naming_the_endpoint_goes_on(self, make_provider, mcp_client_info):
        """A trailing slash names the same resource."""
        provider = make_provider()
        await provider.register_client(mcp_client_info)

        assert request_id_in(await authorize(provider, mcp_client_info, resource=f'{GATEWAY_RESOURCE}/'))


class TestTokenOrigin:
    """MCP codes and tokens are honoured only by the server, and against the upstream, that issued them."""

    async def test_a_token_minted_by_one_server_is_refused_by_another(self, make_provider, mcp_client_info):
        """Otherwise one server's token would unlock the upstream credential mapped behind it on another."""
        petstore = make_provider(issuer=GATEWAY_ISSUER)
        inventory = make_provider(issuer=OTHER_GATEWAY_ISSUER)
        token = await sign_in(petstore, mcp_client_info)
        # The client is known to both, so only the origin can tell the tokens apart.
        await inventory.register_client(mcp_client_info)

        assert await petstore.load_access_token(token.access_token) is not None
        assert await inventory.load_access_token(token.access_token) is None
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

    async def test_tokens_stored_without_an_origin_are_refused(self, provider, memory_store):
        """A legacy record carries no origin, so the client refreshes or signs in again."""
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
        redirect = await complete_upstream(provider, await begin_upstream(provider, mcp_client_info))
        record = await memory_store.get('mcp_auth_code', code_in(redirect))
        # The same code under a new key carries no upstream token mapping, as if that mapping had expired.
        await memory_store.set('mcp_auth_code', 'mcp_orphan', {**record, 'code': 'mcp_orphan'})
        await memory_store.set_mapping('client', 'mcp-client-123', 'upstream_access_token', 'someone-elses-token')
        auth_code = await provider.load_authorization_code(mcp_client_info, 'mcp_orphan')
        assert auth_code is not None

        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(mcp_client_info, auth_code)
