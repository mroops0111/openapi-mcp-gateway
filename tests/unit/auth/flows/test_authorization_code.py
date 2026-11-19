from openapi_mcp_gateway.auth.assembly import AuthTypeContext
from openapi_mcp_gateway.auth.flows import (
    AuthorizationCodeFlowHandler,
    OAuthFlowContext,
)
from openapi_mcp_gateway.auth.flows.factory import resolve_upstream_oauth_flow
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy
from openapi_mcp_gateway.settings import AuthConfig, UpstreamAuthConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.constants import API_URL, AUTHORIZE_URL, ISSUER, TOKEN_URL
from tests.flows import (
    server_entry,
    spec_with_authorization_code,
)
from tests.oauth import bridge_of, server_of


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
