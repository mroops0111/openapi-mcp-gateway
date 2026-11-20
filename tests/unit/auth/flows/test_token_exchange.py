import typing
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openapi_mcp_gateway.auth.assembly import AuthTypeContext, ServerAuthSetup
from openapi_mcp_gateway.auth.flows import (
    OAuthFlowContext,
    TokenExchangeFlowHandler,
    build_oauth_flow,
)
from openapi_mcp_gateway.auth.flows.token_exchange import TokenExchangeBridge
from openapi_mcp_gateway.auth.mcp.external_issuer import McpExternalIssuerMetadata, McpExternalIssuerTokenVerifier
from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamNoCredentialAuthResolver,
    UpstreamTokenSourceAuthResolver,
)
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, ISSUER, JWKS_URL, TOKEN_URL
from tests.flow import (
    auth_context,
    build_spec,
    server_entry,
    spec_with_authorization_code,
)


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
        assert isinstance(setup.resolver._token_source, TokenExchangeBridge)
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
        assert isinstance(setup.resolver._token_source, TokenExchangeBridge)
        assert setup.resolver._token_source.upstream.scopes == ['email', 'profile']

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
            build_oauth_flow(auth_context(entry, spec_with_authorization_code()))

        flow_context = build.call_args.args[1]
        assert flow_context.upstream_oauth_flow is None


class TestTokenExchangeBridge:
    """``TokenExchangeBridge`` exchanges the caller's validated MCP token, and only that one."""

    async def test_the_callers_token_is_exchanged(self):
        """The token the transport verified is what reaches the issuer's token endpoint."""
        upstream = MagicMock()
        upstream.exchange = AsyncMock(return_value='upstream-token')
        caller = MagicMock()
        caller.token = 'caller-token'

        with patch('openapi_mcp_gateway.auth.flows.token_exchange.get_access_token', return_value=caller):
            token = await TokenExchangeBridge(upstream).get_token()

        assert token == 'upstream-token'
        upstream.exchange.assert_awaited_once_with('caller-token')

    async def test_no_token_without_an_authenticated_caller(self):
        """Nothing is exchanged, so the upstream refuses the call rather than the gateway serving it anonymously."""
        upstream = MagicMock()
        upstream.exchange = AsyncMock()

        with patch('openapi_mcp_gateway.auth.flows.token_exchange.get_access_token', return_value=None):
            assert await TokenExchangeBridge(upstream).get_token() is None

        upstream.exchange.assert_not_awaited()


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
    metadata = McpExternalIssuerMetadata(
        issuer=ISSUER,
        jwks_uri=JWKS_URL,
        token_endpoint=TOKEN_URL,
    )
    with (
        patch(
            'openapi_mcp_gateway.auth.flows.token_exchange.fetch_mcp_external_issuer_metadata', return_value=metadata
        ),
        patch('openapi_mcp_gateway.auth.mcp.external_issuer._build_jwk_client'),
    ):
        return TokenExchangeFlowHandler().build(
            OAuthFlowContext(
                AuthTypeContext(
                    entry=entry,
                    spec=build_spec(),
                    store=MemoryTokenStore(),
                    gateway_url='http://localhost:8000',
                    mcp_scope_policy=McpUnrestrictedScopePolicy(),
                    client_metadata_resolver=serving_resolver({}),
                ),
                upstream_oauth_flow=None,
            )
        )
