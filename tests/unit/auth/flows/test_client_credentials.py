from openapi_mcp_gateway.auth.assembly import AuthTypeContext
from openapi_mcp_gateway.auth.flows.client_credentials import UpstreamClientCredentialsFlowHandler
from openapi_mcp_gateway.auth.flows.factory import resolve_upstream_oauth_flow
from openapi_mcp_gateway.auth.upstream.client_credentials import UpstreamClientCredentialsTokenSource
from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamTokenSourceAuthResolver,
)
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy, ScopeRegistry
from openapi_mcp_gateway.settings import AuthConfig, UpstreamAuthConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, TOKEN_URL
from tests.flow import (
    server_entry,
    spec_with_client_credentials,
)


class TestClientCredentialsFlowHandler:
    """Direct tests for the UpstreamClientCredentialsFlowHandler implementation."""

    def test_token_source_uses_resolved_credentials(self):
        """Handler builds a ``UpstreamClientCredentialsTokenSource`` carrying the resolved credentials."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['read'])
            ),
        )
        spec = spec_with_client_credentials()
        handler = UpstreamClientCredentialsFlowHandler()
        flow = resolve_upstream_oauth_flow(entry, spec)

        setup = handler.build(
            AuthTypeContext(
                entry=entry,
                spec=spec,
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                scope_registry=ScopeRegistry(McpUnrestrictedScopePolicy()),
                client_metadata_resolver=serving_resolver({}),
            ),
            flow,
        )

        token_source = setup.resolver._token_source  # type: ignore[attr-defined]
        assert isinstance(token_source, UpstreamClientCredentialsTokenSource)
        assert token_source.client_id == 'cid'
        assert token_source.client_secret == 'sec'
        assert token_source.token_url == TOKEN_URL
        assert token_source.scopes == ['read']


class TestClientCredentialsAudienceWiring:
    """The audience parameters reach the client credentials token source."""

    def test_client_credentials_token_source_receives_resource(self):
        """``UpstreamClientCredentialsFlowHandler`` hands the resolved parameters to the token source."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(
                    type='oauth2', flow='client_credentials', client_id='cid', client_secret='sec', resource=API_URL
                )
            ),
        )
        spec = spec_with_client_credentials()

        setup = UpstreamClientCredentialsFlowHandler().build(
            AuthTypeContext(
                entry=entry,
                spec=spec,
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                scope_registry=ScopeRegistry(McpUnrestrictedScopePolicy()),
                client_metadata_resolver=serving_resolver({}),
            ),
            resolve_upstream_oauth_flow(entry, spec),
        )

        assert isinstance(setup.resolver, UpstreamTokenSourceAuthResolver)
        token_source = setup.resolver._token_source
        assert isinstance(token_source, UpstreamClientCredentialsTokenSource)
        assert token_source.audience_params == {'resource': API_URL}
