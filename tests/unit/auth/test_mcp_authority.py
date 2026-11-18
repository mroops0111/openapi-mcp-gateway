import pytest

from openapi_mcp_gateway.auth.factory import build_auth
from openapi_mcp_gateway.auth.mcp.client_credentials import (
    McpClientCredentialsExtension,
    McpClientCredentialsRoutes,
    McpGatewayTokenVerifier,
)
from openapi_mcp_gateway.auth.mcp_authority import (
    MCP_TOKEN_AUTHORITIES,
    McpClientCredentialsAuthority,
    McpNoTokenAuthority,
)
from openapi_mcp_gateway.settings import AuthConfig
from tests.flows import auth_context, build_spec, server_entry, spec_with_client_credentials


# One agent declared with a secret, as a shared-credential server needs.
AGENT = {'agent': {'client_secret': 'secret'}}


class TestAuthoritySelection:
    """``build_auth`` gives a server whose upstream strategy left the MCP half open the first authority that applies."""

    def test_the_default_comes_last(self):
        assert MCP_TOKEN_AUTHORITIES[-1] is McpNoTokenAuthority

    def test_declared_clients_select_client_credentials(self):
        auth = AuthConfig.model_validate({'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': AGENT}})
        assert McpClientCredentialsAuthority.applies_to(auth)

    @pytest.mark.parametrize(
        'upstream',
        [
            {'type': 'none'},
            {'type': 'bearer', 'token': 't'},
            {'type': 'api_key', 'token': 'k'},
            {'type': 'oauth2', 'flow': 'client_credentials', 'client_id': 'cid', 'client_secret': 'sec'},
        ],
    )
    def test_a_shared_credential_server_with_clients_issues_tokens(self, upstream):
        spec = spec_with_client_credentials() if upstream['type'] == 'oauth2' else build_spec()
        setup = build_auth(auth_context(_build_entry({'upstream': upstream, 'mcp': {'clients': AGENT}}), spec))

        assert isinstance(setup.verifier, McpGatewayTokenVerifier)
        assert setup.provider is None
        assert any(isinstance(routes, McpClientCredentialsRoutes) for routes in setup.routes)
        assert any(isinstance(extension, McpClientCredentialsExtension) for extension in setup.extensions)
        assert setup.authorization_server_metadata is not None
        assert setup.authorization_server_metadata.grant_types_supported == ['client_credentials']
        assert not setup.mcp_tokens_carry_upstream_grant
        assert setup.mcp_summary == 'gateway (client_credentials)'
        # The upstream half survives the MCP half being attached.
        assert setup.flow_type == upstream.get('flow')

    def test_a_server_without_clients_stays_open(self):
        setup = build_auth(auth_context(_build_entry({'upstream': {'type': 'bearer', 'token': 't'}}), build_spec()))

        assert setup.verifier is None
        assert setup.authorization_server_metadata is None
        assert setup.routes == []

    def test_a_resolved_client_credentials_flow_checks_its_clients(self):
        """An unset flow defers the check to startup, once the spec has resolved it."""
        entry = _build_entry(
            {
                'upstream': {'type': 'oauth2', 'client_id': 'cid', 'client_secret': 'sec'},
                'mcp': {'clients': {'agent': {}}},
            }
        )
        with pytest.raises(ValueError, match=r'agent\.client_secret is required'):
            build_auth(auth_context(entry, spec_with_client_credentials()))


def _build_entry(auth: dict):
    """A server carrying ``auth``."""
    return server_entry(AuthConfig.model_validate(auth))
