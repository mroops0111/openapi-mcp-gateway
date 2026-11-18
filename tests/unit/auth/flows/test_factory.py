import pytest

from openapi_mcp_gateway.auth.flows import (
    build_oauth_flow,
)
from openapi_mcp_gateway.auth.flows.authorization_code import AuthorizationCodeBridge
from openapi_mcp_gateway.auth.flows.factory import resolve_upstream_oauth_flow
from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamTokenSourceAuthResolver,
)
from openapi_mcp_gateway.settings import AuthConfig, McpAuthConfig, UpstreamAuthConfig
from tests.constants import AUTHORIZE_URL, TOKEN_URL
from tests.flows import (
    auth_context,
    build_spec,
    server_entry,
    spec_with_authorization_code,
    spec_with_both_flows,
    spec_with_client_credentials,
)
from tests.oauth import server_of


class TestResolveOAuthFlow:
    """``resolve_upstream_oauth_flow`` selects between spec-declared and config-supplied flows."""

    def test_picks_authorization_code_by_default(self):
        """When the spec declares both flows, authorization_code wins."""
        entry = server_entry(
            AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        )
        flow = resolve_upstream_oauth_flow(entry, spec_with_both_flows())
        assert flow.flow_type == 'authorization_code'

    def test_picks_client_credentials_when_only_one_declared(self):
        """If the spec only declares clientCredentials, it is selected without an override."""
        entry = server_entry(
            AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        )
        flow = resolve_upstream_oauth_flow(entry, spec_with_client_credentials())
        assert flow.flow_type == 'client_credentials'

    def test_explicit_flow_wins_over_default(self):
        """``auth.upstream.flow='client_credentials'`` overrides the authorization_code preference."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(
                    type='oauth2', flow='client_credentials', client_id='cid', client_secret='sec'
                )
            ),
        )
        flow = resolve_upstream_oauth_flow(entry, spec_with_both_flows())
        assert flow.flow_type == 'client_credentials'

    def test_explicit_flow_unknown_in_spec_raises(self):
        """Asking for a flow the spec does not declare is a configuration error."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(
                    type='oauth2', flow='client_credentials', client_id='cid', client_secret='sec'
                )
            ),
        )
        with pytest.raises(ValueError, match='spec does not declare'):
            resolve_upstream_oauth_flow(entry, spec_with_authorization_code())

    def test_synthesises_from_config_when_spec_has_none(self):
        """With no declared flows and an explicit token_url, client_credentials is synthesised by default."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', token_url=TOKEN_URL)
            ),
        )
        flow = resolve_upstream_oauth_flow(entry, build_spec())
        assert flow.flow_type == 'client_credentials'
        assert flow.token_url == TOKEN_URL

    def test_synthesises_authorization_code_when_authorization_url_set(self):
        """With no declared flows but both URLs set, authorization_code is inferred."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(
                    type='oauth2',
                    client_id='cid',
                    client_secret='sec',
                    authorization_url=AUTHORIZE_URL,
                    token_url=TOKEN_URL,
                )
            ),
        )
        flow = resolve_upstream_oauth_flow(entry, build_spec())
        assert flow.flow_type == 'authorization_code'

    def test_no_spec_no_token_url_raises(self):
        """OAuth2 with neither spec flow nor token_url is rejected with a clear error."""
        entry = server_entry(
            AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        )
        with pytest.raises(ValueError, match='no OAuth2 flow'):
            resolve_upstream_oauth_flow(entry, build_spec())

    def test_config_urls_override_spec_urls(self):
        """Explicit ``auth.upstream.token_url`` overrides the spec-declared value."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(
                    type='oauth2', client_id='cid', client_secret='sec', token_url='https://other.example.com/token'
                )
            ),
        )
        flow = resolve_upstream_oauth_flow(entry, spec_with_authorization_code())
        assert flow.token_url == 'https://other.example.com/token'


class TestBuildOAuthFlow:
    """``build_oauth_flow`` dispatches to the right handler and returns a populated setup."""

    def test_authorization_code_returns_provider_and_settings(self):
        """authorization_code yields a provider, AuthSettings, and a resolver reading the Bridge."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api'])
            ),
        )
        setup = build_oauth_flow(auth_context(entry, spec_with_authorization_code()))
        assert isinstance(setup.resolver, UpstreamTokenSourceAuthResolver)
        assert isinstance(setup.resolver._token_source, AuthorizationCodeBridge)
        assert setup.provider is not None
        assert setup.settings is not None
        assert setup.on_shutdown is None

    def test_authorization_code_defaults_token_ttls(self):
        """Without config overrides the provider keeps the built-in access and refresh TTLs."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api'])
            ),
        )
        setup = build_oauth_flow(auth_context(entry, spec_with_authorization_code()))
        assert setup.provider is not None
        assert server_of(setup.provider).tokens.policy.access_token_ttl == 3600
        assert server_of(setup.provider).tokens.policy.refresh_token_ttl == 86400

    def test_authorization_code_honours_configured_token_ttls(self):
        """``auth.mcp.access_token_ttl`` and ``refresh_token_ttl`` reach the provider."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api']),
                mcp=McpAuthConfig(access_token_ttl=7200, refresh_token_ttl=604800),
            ),
        )
        setup = build_oauth_flow(auth_context(entry, spec_with_authorization_code()))
        assert setup.provider is not None
        assert server_of(setup.provider).tokens.policy.access_token_ttl == 7200
        assert server_of(setup.provider).tokens.policy.refresh_token_ttl == 604800

    def test_client_credentials_returns_token_source_resolver(self):
        """client_credentials yields a UpstreamTokenSourceAuthResolver and an on_shutdown hook."""
        entry = server_entry(
            AuthConfig(
                upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api'])
            ),
        )
        setup = build_oauth_flow(auth_context(entry, spec_with_client_credentials()))
        assert isinstance(setup.resolver, UpstreamTokenSourceAuthResolver)
        assert setup.provider is None
        assert setup.settings is None
        assert setup.on_shutdown is not None

    def test_client_credentials_requires_credentials(self):
        """Missing client_id/client_secret on client_credentials raises ``ValueError``."""
        entry = server_entry(AuthConfig(upstream=UpstreamAuthConfig(type='oauth2')))
        with pytest.raises(ValueError, match='client_id and client_secret'):
            build_oauth_flow(auth_context(entry, spec_with_client_credentials()))

    def test_authorization_code_without_creds_raises(self):
        """Auto-detected authorization_code without client credentials fails fast.

        Previously the resolver silently fell back to passthrough,
        which forwards the MCP client's token to a third-party upstream.
        That violates RFC 8707 audience binding, the "confused deputy" pattern the MCP authorization spec forbids.

        Users who genuinely want passthrough must set ``auth.upstream.type='passthrough'`` explicitly,
        to acknowledge the shared-audience requirement.
        """
        entry = server_entry(AuthConfig(upstream=UpstreamAuthConfig(type='oauth2')))
        with pytest.raises(ValueError, match='client_id/client_secret'):
            build_oauth_flow(auth_context(entry, spec_with_authorization_code()))

    def test_explicit_authorization_code_without_creds_raises(self):
        """Forcing ``flow='authorization_code'`` without credentials still fails fast."""
        entry = server_entry(AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', flow='authorization_code')))
        with pytest.raises(ValueError, match='client_id and client_secret'):
            build_oauth_flow(auth_context(entry, spec_with_authorization_code()))
