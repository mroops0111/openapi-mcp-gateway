import typing
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openapi_mcp_gateway.auth.assembly import AuthTypeContext, ServerAuthSetup
from openapi_mcp_gateway.auth.flows import (
    TokenExchangeFlowHandler,
)
from openapi_mcp_gateway.auth.flows.token_exchange import TokenExchangeBridge
from openapi_mcp_gateway.auth.mcp.external_issuer import McpExternalIssuerTokenVerifier
from openapi_mcp_gateway.auth.upstream.resolver import (
    UPSTREAM_CREDENTIAL_UNAVAILABLE,
    UpstreamNoCredentialAuthResolver,
    UpstreamTokenSourceAuthResolver,
)
from openapi_mcp_gateway.auth.upstream.token_exchange import (
    UpstreamExchangedToken,
    UpstreamTokenExchangeClient,
    UpstreamTokenExchangeError,
    UpstreamTokenExchangeRefusedError,
)
from openapi_mcp_gateway.scopes import (
    McpAccessToken,
    McpUnrestrictedScopePolicy,
    ScopeRegistry,
    UpstreamGrantUnavailableError,
)
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, ISSUER, TOKEN_URL
from tests.context import authenticated_as
from tests.flow import (
    auth_context,
    build_selected_oauth_flow,
    build_spec,
    discovered_external_issuer,
    server_entry,
    spec_with_authorization_code,
)
from tests.scope import SECURED_CEILING, secured_registry
from tests.token_endpoint import TokenEndpoint


class TestTokenExchangeFlowHandler:
    """The token_exchange flow delegates issuance and exchanges the caller's token."""

    def test_advertises_the_gateway_as_the_resource_and_the_issuer_as_the_as(self):
        """The document names this endpoint's canonical URI, and the external issuer that mints for it.

        Advertising the upstream's identifier instead would have clients request a token the gateway must refuse,
        since the MCP spec forbids accepting one minted for another resource.
        """
        setup = _build_token_exchange(_token_exchange_entry())

        assert setup.protected_resource is not None
        assert str(setup.protected_resource.resource) == 'http://localhost:8000/srv/mcp'
        assert [str(server) for server in setup.protected_resource.authorization_servers] == [ISSUER]

    def test_produces_a_verifier_and_no_provider(self):
        """The gateway validates rather than issues, which is what the SDK's two modes are."""
        setup = _build_token_exchange(_token_exchange_entry())

        assert setup.provider is None
        assert setup.verifier is not None
        assert isinstance(setup.resolver, UpstreamTokenSourceAuthResolver)
        assert isinstance(setup.resolver.token_source, TokenExchangeBridge)
        assert setup.mcp_summary == f'issuer ({ISSUER})'

    def test_verifier_audience_is_the_gateway_not_the_upstream(self):
        """Inbound tokens must name this endpoint, even though the exchange targets the upstream."""
        setup = _build_token_exchange(_token_exchange_entry())

        assert isinstance(setup.verifier, McpExternalIssuerTokenVerifier)
        assert setup.verifier.audience == 'http://localhost:8000/srv/mcp'

    def test_requires_issuer(self):
        """Without an issuer there is nothing to validate tokens against."""
        with pytest.raises(ValueError, match=r'requires auth\.mcp\.issuer'):
            _build_token_exchange(_token_exchange_entry(issuer=None))

    def test_requires_an_upstream_audience(self):
        """Without a target the issuer mints for its own default, which the upstream refuses."""
        with pytest.raises(ValueError, match=r'requires auth\.upstream\.resource or auth\.upstream\.audience'):
            _build_token_exchange(_token_exchange_entry(audience=None))

    def test_inbound_and_upstream_scopes_are_independent(self):
        """Asking the issuer for a scope must not make it mandatory on the caller's token.

        A deployment chooses what to request upstream, but has no say in what a caller's client registered with.
        Coupling the two locked out every client that did not happen to match.
        """
        setup = _build_token_exchange(
            _token_exchange_entry(scopes=['email', 'profile'], required_scopes=['mcp.access'])
        )

        assert isinstance(setup.verifier, McpExternalIssuerTokenVerifier)
        assert setup.verifier.required_scopes == ('mcp.access',)
        assert isinstance(setup.resolver, UpstreamTokenSourceAuthResolver)
        assert isinstance(setup.resolver.token_source, TokenExchangeBridge)
        assert setup.resolver.token_source.ceiling == ['email', 'profile']

    def test_advertised_scopes_are_the_ones_a_caller_must_hold(self):
        """RFC 9728 ``scopes_supported`` tells a client what to register for, so it is the inbound set.

        Advertising the upstream set instead would send clients after scopes this endpoint never checks,
        while staying silent about the ones it does.
        """
        setup = _build_token_exchange(_token_exchange_entry(scopes=['email'], required_scopes=['mcp.access']))

        assert setup.protected_resource is not None
        assert setup.protected_resource.scopes_supported == ['mcp.access']

    def test_requires_client_credentials_for_the_exchange(self):
        """Authorization servers require a confidential client for the token-exchange grant."""
        with pytest.raises(ValueError, match='requires client_id and client_secret'):
            _build_token_exchange(_token_exchange_entry(client_secret=None))

    def test_resolves_no_upstream_flow_from_the_spec(self):
        """``securitySchemes`` cannot declare this flow, so the spec's flows are never consulted.

        Consulting them would raise a misleading "flow not declared" error on every spec.
        """
        entry = _token_exchange_entry()
        setup = ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver())
        handler_build = patch.object(TokenExchangeFlowHandler, 'build', autospec=True, return_value=setup)

        with handler_build as build:
            build_selected_oauth_flow(auth_context(entry, spec_with_authorization_code()))

        assert build.call_args.args[2] is None


class TestTokenExchangeBridge:
    """``TokenExchangeBridge`` exchanges the caller's validated MCP token, and only that one."""

    async def test_the_callers_token_is_exchanged(self):
        """The token the transport verified is what reaches the issuer's token endpoint."""
        upstream = _exchange_client(UpstreamExchangedToken('upstream-token', ()))

        with authenticated_as(_caller(['products:read'])):
            token = await _bridge(upstream).get_token()

        assert token == 'upstream-token'
        assert upstream.exchange.await_args is not None
        assert upstream.exchange.await_args.args[0] == 'caller-token'

    async def test_no_token_without_an_authenticated_caller(self):
        """Nothing is exchanged, so the upstream refuses the call rather than the gateway serving it anonymously."""
        upstream = _exchange_client(UpstreamExchangedToken('upstream-token', ()))

        with authenticated_as(None):
            assert await _bridge(upstream).get_token() is None

        upstream.exchange.assert_not_awaited()

    @pytest.mark.parametrize(
        ('mcp_scopes', 'asked'),
        [
            (['products:read'], ['read:products']),
            (['products:delete'], ['read:products', 'write:products']),
            (['openid'], []),
        ],
    )
    async def test_each_exchange_asks_for_what_the_callers_mcp_scopes_open(self, mcp_scopes, asked):
        """An exchange asks the issuer for no more than a sign-in under the same MCP scopes would."""
        upstream = _exchange_client(UpstreamExchangedToken('upstream-token', ()))

        with authenticated_as(_caller(mcp_scopes)):
            await _bridge(upstream).get_token()

        upstream.exchange.assert_awaited_once_with('caller-token', asked)

    async def test_the_call_grant_is_what_the_exchange_granted(self):
        upstream = _exchange_client(UpstreamExchangedToken('upstream-token', ('read:products',)))

        with authenticated_as(_caller(['products:read'])):
            grant = await _bridge(upstream).read_call_grant()

        assert grant.mcp_scopes == {'products:read'}
        assert grant.upstream_scopes == {'read:products'}

    async def test_a_refused_exchange_leaves_the_grant_unavailable_for_the_issuers_reason(self):
        """An issuer that will not grant every scope may refuse the whole exchange, as Keycloak does."""
        upstream = MagicMock()
        upstream.exchange = AsyncMock(side_effect=UpstreamTokenExchangeRefusedError('refused (invalid_scope)'))

        with (
            authenticated_as(_caller(['products:write'])),
            pytest.raises(UpstreamGrantUnavailableError, match='invalid_scope'),
        ):
            await _bridge(upstream).read_call_grant()

    @pytest.mark.parametrize(
        'failure',
        [UpstreamTokenExchangeError('Token exchange failed (401): bad secret'), httpx.ConnectError('unreachable')],
        ids=['failed exchange', 'issuer unreachable'],
    )
    async def test_any_other_failure_leaves_the_grant_unavailable_without_its_detail(self, failure):
        """The failure is the gateway's, so the caller is not shown the issuer's response."""
        upstream = MagicMock()
        upstream.exchange = AsyncMock(side_effect=failure)

        with (
            authenticated_as(_caller(['products:write'])),
            pytest.raises(UpstreamGrantUnavailableError) as raised,
        ):
            await _bridge(upstream).read_call_grant()

        assert str(raised.value) == UPSTREAM_CREDENTIAL_UNAVAILABLE

    async def test_a_call_grant_and_its_token_share_one_exchange(self):
        """The grant is read before the call and the token on it, and the exchange client serves both from one answer."""
        endpoint = TokenEndpoint(
            httpx.Response(200, json={'access_token': 'upstream-token', 'scope': 'read:products', 'expires_in': 3600})
        )
        upstream = UpstreamTokenExchangeClient(
            token_endpoint=TOKEN_URL, client_id='gateway', client_secret='secret', transport=endpoint
        )
        bridge = _bridge(upstream)

        with authenticated_as(_caller(['products:read'])):
            await bridge.read_call_grant()
            await bridge.get_token()

        assert len(endpoint.forms) == 1


def _token_exchange_entry(**overrides) -> ServerConfig:
    """Entry configured for the token_exchange flow, with every requirement satisfied by default.

    Keys of both the ``upstream`` and ``mcp`` blocks are accepted flat here for brevity,
    so a test can vary one of them without restating the rest.
    """
    mcp_keys = {'issuer', 'required_scopes'}
    upstream: dict[str, typing.Any] = {
        'type': 'oauth2',
        'flow': 'token_exchange',
        'audience': API_URL,
        'client_id': 'gateway',
        'client_secret': 'secret',
    }
    mcp: dict[str, typing.Any] = {'issuer': ISSUER}
    for key, value in overrides.items():
        (mcp if key in mcp_keys else upstream)[key] = value
    return server_entry(AuthConfig.model_validate({'upstream': upstream, 'mcp': mcp}))


def _build_token_exchange(entry: ServerConfig):
    """Run the handler with issuer discovery stubbed out."""
    with discovered_external_issuer():
        return TokenExchangeFlowHandler().build(
            AuthTypeContext(
                entry=entry,
                spec=build_spec(),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                scope_registry=ScopeRegistry(McpUnrestrictedScopePolicy()),
                client_metadata_resolver=serving_resolver({}),
            ),
            None,
        )


def _bridge(upstream: typing.Any) -> TokenExchangeBridge:
    """A bridge over the secured shop, whose ceiling is every scope its operations declare."""
    return TokenExchangeBridge(upstream, secured_registry(), SECURED_CEILING, [])


def _exchange_client(exchanged: UpstreamExchangedToken) -> MagicMock:
    """An exchange client answering every exchange with ``exchanged``."""
    upstream = MagicMock()
    upstream.exchange = AsyncMock(return_value=exchanged)
    return upstream


def _caller(mcp_scopes: list[str]) -> McpAccessToken:
    """The token the transport verified for a caller holding ``mcp_scopes``."""
    return McpAccessToken(token='caller-token', client_id='agent', scopes=mcp_scopes, upstream_scopes=None)
