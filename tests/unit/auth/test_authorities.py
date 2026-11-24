import pytest

from openapi_mcp_gateway.auth.authorities import (
    MCP_TOKEN_AUTHORITIES,
    McpClientCredentialsAuthority,
    McpNoTokenAuthority,
)
from openapi_mcp_gateway.auth.factory import build_auth
from openapi_mcp_gateway.auth.mcp.client_credentials import (
    McpClientCredentialsExtension,
    McpClientCredentialsRoutes,
)
from openapi_mcp_gateway.auth.mcp.token_issuer import McpGatewayIssuerTokenVerifier
from openapi_mcp_gateway.settings import AuthConfig
from tests.flow import auth_context, build_spec, server_entry, spec_with_client_credentials


# One agent declared with a secret, as a shared-credential server needs.
AGENT = {'agent': {'client_secret': 'secret'}}


class TestAuthoritySelection:
    """``build_auth`` gives a server whose upstream strategy left the MCP half open the first authority that applies."""

    def test_the_default_comes_last(self):
        assert MCP_TOKEN_AUTHORITIES[-1] is McpNoTokenAuthority

    def test_declared_clients_select_client_credentials(self):
        auth = AuthConfig.model_validate({'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': AGENT}})
        assert McpClientCredentialsAuthority().applies_to(auth, 'gateway')

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

        assert isinstance(setup.verifier, McpGatewayIssuerTokenVerifier)
        assert setup.provider is None
        assert any(isinstance(routes, McpClientCredentialsRoutes) for routes in setup.routes)
        assert any(isinstance(extension, McpClientCredentialsExtension) for extension in setup.extensions)
        assert setup.authorization_server_metadata is not None
        assert setup.authorization_server_metadata.grant_types_supported == ['client_credentials']
        assert not setup.mcp_tokens_carry_upstream_grant
        assert setup.mcp_summary == 'gateway (client_credentials)'
        # The upstream half survives the MCP half being attached.
        assert setup.flow_type == upstream.get('flow')

    def test_discovery_names_the_endpoint_scopes(self):
        """A client asking for fewer than these would be refused on every call."""
        entry = _build_entry(
            {'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': AGENT, 'required_scopes': ['api', 'mcp']}}
        )
        setup = build_auth(auth_context(entry, build_spec()))

        assert setup.authorization_server_metadata is not None
        assert setup.authorization_server_metadata.scopes_supported == ['api', 'mcp']
        assert setup.protected_resource is not None
        assert setup.protected_resource.scopes_supported == ['api', 'mcp']

    def test_a_server_without_clients_stays_open(self):
        setup = build_auth(auth_context(_build_entry({'upstream': {'type': 'bearer', 'token': 't'}}), build_spec()))

        assert setup.verifier is None
        assert setup.authorization_server_metadata is None
        assert setup.routes == []

    @pytest.mark.parametrize('principal', ['user', 'caller'])
    def test_clients_select_nothing_where_the_upstream_sees_someone_else(self, principal):
        auth = AuthConfig.model_validate({'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': AGENT}})
        assert not McpClientCredentialsAuthority().applies_to(auth, principal)

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


class TestClientSettingsWithoutSignIn:
    """Agents in front of one shared upstream credential obtain tokens as themselves, so no user ever signs in."""

    @pytest.mark.parametrize(
        ('client', 'refusal'),
        [
            ({}, r'agent\.client_secret is required'),
            ({'client_secret': 's', 'redirect_uris': ['http://localhost/cb']}, r'agent\.redirect_uris has no effect'),
            ({'client_secret': 's', 'default_scopes': []}, r'agent\.default_scopes has no effect'),
            ({'client_secret': 's', 'consent': False}, r'agent\.consent has no effect'),
        ],
    )
    def test_sign_in_settings_are_refused(self, client, refusal):
        entry = _build_entry({'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': {'agent': client}}})
        with pytest.raises(ValueError, match=refusal):
            build_auth(auth_context(entry, build_spec()))

    def test_a_metadata_document_client_is_refused(self):
        """Such a client signs a user in through a browser, which never happens here."""
        client_id = 'https://app.example.com/client.json'
        entry = _build_entry({'upstream': {'type': 'bearer', 'token': 't'}, 'mcp': {'clients': {client_id: {}}}})
        with pytest.raises(ValueError, match=r'names a Client ID Metadata Document, whose client signs a user in'):
            build_auth(auth_context(entry, build_spec()))

    def test_a_refresh_token_ttl_is_refused(self):
        """No refresh token is issued, since the agent can always ask for a new token."""
        entry = _build_entry({'upstream': {'type': 'none'}, 'mcp': {'clients': AGENT, 'refresh_token_ttl': 600}})
        with pytest.raises(
            ValueError, match=r'auth\.mcp\.refresh_token_ttl needs auth\.upstream\.flow authorization_code'
        ):
            build_auth(auth_context(entry, build_spec()))


def _build_entry(auth: dict):
    """A server carrying ``auth``."""
    return server_entry(AuthConfig.model_validate(auth))
