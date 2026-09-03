from unittest.mock import patch

import pytest

from openapi_mcp_gateway.auth.flows import (
    AuthorizationCodeFlowHandler,
    ClientCredentialsFlowHandler,
    OAuthFlowContext,
    PassthroughFlowHandler,
    ResourceServerFlowHandler,
    build_oauth_flow,
)
from openapi_mcp_gateway.auth.flows.factory import resolve_oauth_flow
from openapi_mcp_gateway.auth.oidc import IssuerMetadata
from openapi_mcp_gateway.auth.resolver import (
    AuthorizationCodeAuthResolver,
    PassthroughAuthResolver,
    TokenExchangeAuthResolver,
    TokenSourceAuthResolver,
)
from openapi_mcp_gateway.auth.token_source import ClientCredentialsTokenSource
from openapi_mcp_gateway.openapi import OpenAPISpec
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore


def _build_spec(security_schemes: dict | None = None) -> OpenAPISpec:
    """Construct a minimal ``OpenAPISpec`` carrying the given ``securitySchemes``."""
    return OpenAPISpec(
        raw={},
        title='test',
        version='1.0.0',
        operations=[],
        security_schemes=security_schemes or {},
    )


def _spec_with_authorization_code() -> OpenAPISpec:
    """Spec declaring only the ``authorization_code`` flow."""
    return _build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': 'https://auth.example.com/authorize',
                        'tokenUrl': 'https://auth.example.com/token',
                        'scopes': {'read': 'r', 'write': 'w'},
                    },
                },
            },
        }
    )


def _spec_with_client_credentials() -> OpenAPISpec:
    """Spec declaring only the ``client_credentials`` flow."""
    return _build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'clientCredentials': {
                        'tokenUrl': 'https://auth.example.com/token',
                        'scopes': {'api': 'api'},
                    },
                },
            },
        }
    )


def _spec_with_both_flows() -> OpenAPISpec:
    """Spec declaring both ``authorization_code`` and ``client_credentials``."""
    return _build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': 'https://auth.example.com/authorize',
                        'tokenUrl': 'https://auth.example.com/token',
                        'scopes': {'api': 'api'},
                    },
                    'clientCredentials': {
                        'tokenUrl': 'https://auth.example.com/token',
                        'scopes': {'api': 'api'},
                    },
                },
            },
        }
    )


def _entry(auth: AuthConfig, name: str = 'srv') -> ServerConfig:
    """Minimal ``ServerConfig`` carrying ``auth`` for factory tests."""
    return ServerConfig(name=name, spec='dummy.json', base_url='https://api.example.com', auth=auth)


class TestResolveOAuthFlow:
    """``resolve_oauth_flow`` selects between spec-declared and config-supplied flows."""

    def test_picks_authorization_code_by_default(self):
        """When the spec declares both flows, authorization_code wins."""
        entry = _entry(AuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        flow = resolve_oauth_flow(entry, _spec_with_both_flows())
        assert flow.flow_type == 'authorization_code'

    def test_picks_client_credentials_when_only_one_declared(self):
        """If the spec only declares clientCredentials, it is selected without an override."""
        entry = _entry(AuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        flow = resolve_oauth_flow(entry, _spec_with_client_credentials())
        assert flow.flow_type == 'client_credentials'

    def test_explicit_flow_wins_over_default(self):
        """``auth.flow='client_credentials'`` overrides the authorization_code preference."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', flow='client_credentials'),
        )
        flow = resolve_oauth_flow(entry, _spec_with_both_flows())
        assert flow.flow_type == 'client_credentials'

    def test_explicit_flow_unknown_in_spec_raises(self):
        """Asking for a flow the spec does not declare is a configuration error."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', flow='client_credentials'),
        )
        with pytest.raises(ValueError, match='spec does not declare'):
            resolve_oauth_flow(entry, _spec_with_authorization_code())

    def test_synthesises_from_config_when_spec_has_none(self):
        """No declared flows + explicit token_url → synthesised client_credentials by default."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                token_url='https://auth.example.com/token',
            ),
        )
        flow = resolve_oauth_flow(entry, _build_spec())
        assert flow.flow_type == 'client_credentials'
        assert flow.token_url == 'https://auth.example.com/token'

    def test_synthesises_authorization_code_when_authorization_url_set(self):
        """No declared flows but both URLs set → authorization_code is inferred."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                authorization_url='https://auth.example.com/authorize',
                token_url='https://auth.example.com/token',
            ),
        )
        flow = resolve_oauth_flow(entry, _build_spec())
        assert flow.flow_type == 'authorization_code'

    def test_no_spec_no_token_url_raises(self):
        """OAuth2 with neither spec flow nor token_url is rejected with a clear error."""
        entry = _entry(AuthConfig(type='oauth2', client_id='cid', client_secret='sec'))
        with pytest.raises(ValueError, match='no OAuth2 flow'):
            resolve_oauth_flow(entry, _build_spec())

    def test_config_urls_override_spec_urls(self):
        """Explicit ``auth.token_url`` overrides the spec-declared value."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                token_url='https://other.example.com/token',
            ),
        )
        flow = resolve_oauth_flow(entry, _spec_with_authorization_code())
        assert flow.token_url == 'https://other.example.com/token'


class TestBuildOAuthFlow:
    """``build_oauth_flow`` dispatches to the right handler and returns a populated setup."""

    def test_authorization_code_returns_provider_and_settings(self):
        """authorization_code yields a provider, AuthSettings, and an AuthorizationCodeAuthResolver."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api']),
        )
        setup = build_oauth_flow(
            entry=entry,
            spec=_spec_with_authorization_code(),
            store=MemoryTokenStore(),
            gateway_url='http://localhost:8000',
            mount_path='/srv',
        )
        assert isinstance(setup.resolver, AuthorizationCodeAuthResolver)
        assert setup.provider is not None
        assert setup.settings is not None
        assert setup.on_shutdown is None

    def test_authorization_code_defaults_token_ttls(self):
        """Without config overrides the provider keeps the built-in access and refresh TTLs."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api']),
        )
        setup = build_oauth_flow(
            entry=entry,
            spec=_spec_with_authorization_code(),
            store=MemoryTokenStore(),
            gateway_url='http://localhost:8000',
            mount_path='/srv',
        )
        assert setup.provider is not None
        assert setup.provider.mcp_access_token_ttl == 3600
        assert setup.provider.mcp_refresh_token_ttl == 86400

    def test_authorization_code_honours_configured_token_ttls(self):
        """``mcp_access_token_ttl`` / ``mcp_refresh_token_ttl`` reach the provider."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                scopes=['api'],
                mcp_access_token_ttl=7200,
                mcp_refresh_token_ttl=604800,
            ),
        )
        setup = build_oauth_flow(
            entry=entry,
            spec=_spec_with_authorization_code(),
            store=MemoryTokenStore(),
            gateway_url='http://localhost:8000',
            mount_path='/srv',
        )
        assert setup.provider is not None
        assert setup.provider.mcp_access_token_ttl == 7200
        assert setup.provider.mcp_refresh_token_ttl == 604800

    def test_client_credentials_returns_token_source_resolver(self):
        """client_credentials yields a TokenSourceAuthResolver and an on_shutdown hook."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api']),
        )
        setup = build_oauth_flow(
            entry=entry,
            spec=_spec_with_client_credentials(),
            store=MemoryTokenStore(),
            gateway_url='http://localhost:8000',
            mount_path='/srv',
        )
        assert isinstance(setup.resolver, TokenSourceAuthResolver)
        assert setup.provider is None
        assert setup.settings is None
        assert setup.on_shutdown is not None

    def test_client_credentials_requires_credentials(self):
        """Missing client_id/client_secret on client_credentials raises ``ValueError``."""
        entry = _entry(AuthConfig(type='oauth2'))
        with pytest.raises(ValueError, match='client_id and client_secret'):
            build_oauth_flow(
                entry=entry,
                spec=_spec_with_client_credentials(),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )

    def test_authorization_code_without_creds_raises(self):
        """Auto-detected authorization_code without client credentials fails fast.

        Previously the resolver silently fell back to passthrough,
        which forwards the MCP client's token to a third-party upstream.
        That violates RFC 8707 audience binding, the "confused deputy" pattern the MCP authorization spec forbids.

        Users who genuinely want passthrough must set ``auth.flow='passthrough'`` explicitly,
        to acknowledge the shared-audience requirement.
        """
        entry = _entry(AuthConfig(type='oauth2'))
        with pytest.raises(ValueError, match='client_id/client_secret'):
            build_oauth_flow(
                entry=entry,
                spec=_spec_with_authorization_code(),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )

    def test_explicit_authorization_code_without_creds_raises(self):
        """Forcing ``flow='authorization_code'`` without credentials still fails fast."""
        entry = _entry(AuthConfig(type='oauth2', flow='authorization_code'))
        with pytest.raises(ValueError, match='client_id and client_secret'):
            build_oauth_flow(
                entry=entry,
                spec=_spec_with_authorization_code(),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )

    def test_explicit_passthrough_short_circuits_detector(self):
        """``flow='passthrough'`` is honoured even when the spec declares no OAuth flows."""
        entry = _entry(AuthConfig(type='oauth2', flow='passthrough'))
        setup = build_oauth_flow(
            entry=entry,
            spec=_build_spec(),
            store=MemoryTokenStore(),
            gateway_url='http://localhost:8000',
            mount_path='/srv',
        )
        assert isinstance(setup.resolver, PassthroughAuthResolver)


class TestPassthroughFlowHandler:
    """``PassthroughFlowHandler`` builds a minimal setup (resolver only)."""

    def test_build_returns_passthrough_resolver(self):
        """The handler returns an ``OAuthFlowSetup`` carrying only a ``PassthroughAuthResolver``."""
        from openapi_mcp_gateway.auth.detector import DetectedOAuthFlow

        entry = _entry(AuthConfig(type='oauth2', flow='passthrough'))
        flow = DetectedOAuthFlow(flow_type='passthrough')
        setup = PassthroughFlowHandler().build(
            OAuthFlowContext(
                entry=entry,
                spec=_build_spec(),
                oauth_flow=flow,
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )
        )
        assert isinstance(setup.resolver, PassthroughAuthResolver)
        assert setup.provider is None
        assert setup.settings is None
        assert setup.on_shutdown is None


class TestClientCredentialsFlowHandler:
    """Direct tests for the ClientCredentialsFlowHandler implementation."""

    def test_token_source_uses_resolved_credentials(self):
        """Handler builds a ``ClientCredentialsTokenSource`` carrying the resolved credentials."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['read']),
        )
        spec = _spec_with_client_credentials()
        handler = ClientCredentialsFlowHandler()
        flow = resolve_oauth_flow(entry, spec)

        setup = handler.build(
            OAuthFlowContext(
                entry=entry,
                spec=spec,
                oauth_flow=flow,
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )
        )

        token_source = setup.resolver._token_source  # type: ignore[attr-defined]
        assert isinstance(token_source, ClientCredentialsTokenSource)
        assert token_source.client_id == 'cid'
        assert token_source.client_secret == 'sec'
        assert token_source.token_url == 'https://auth.example.com/token'
        assert token_source.scopes == ['read']


class TestAuthorizationCodeFlowHandler:
    """Direct tests for the AuthorizationCodeFlowHandler."""

    def test_provider_carries_callback_url(self):
        """Handler composes the callback URL from gateway_url + mount_path."""
        entry = _entry(
            AuthConfig(type='oauth2', client_id='cid', client_secret='sec', scopes=['api']),
        )
        spec = _spec_with_authorization_code()
        handler = AuthorizationCodeFlowHandler()
        flow = resolve_oauth_flow(entry, spec)

        setup = handler.build(
            OAuthFlowContext(
                entry=entry,
                spec=spec,
                oauth_flow=flow,
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000/',
                mount_path='/srv',
            )
        )

        assert setup.provider is not None
        assert setup.provider.callback_url == 'http://localhost:8000/srv/auth/callback'
        assert setup.provider.upstream_auth_url == 'https://auth.example.com/authorize'
        assert setup.provider.upstream_token_url == 'https://auth.example.com/token'


class TestUpstreamAudienceWiring:
    """Config-level ``resource`` / ``audience`` reach the component that talks to the upstream."""

    def test_authorization_code_provider_receives_audience(self):
        """``AuthorizationCodeFlowHandler`` hands the resolved parameters to the provider."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                audience='https://api.example.com',
            ),
        )
        spec = _spec_with_authorization_code()

        setup = AuthorizationCodeFlowHandler().build(
            OAuthFlowContext(
                entry=entry,
                spec=spec,
                oauth_flow=resolve_oauth_flow(entry, spec),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )
        )

        assert setup.provider is not None
        assert setup.provider.audience_params == {'audience': 'https://api.example.com'}

    def test_client_credentials_token_source_receives_resource(self):
        """``ClientCredentialsFlowHandler`` hands the resolved parameters to the token source."""
        entry = _entry(
            AuthConfig(
                type='oauth2',
                client_id='cid',
                client_secret='sec',
                flow='client_credentials',
                resource='https://api.example.com',
            ),
        )
        spec = _spec_with_client_credentials()

        setup = ClientCredentialsFlowHandler().build(
            OAuthFlowContext(
                entry=entry,
                spec=spec,
                oauth_flow=resolve_oauth_flow(entry, spec),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )
        )

        assert isinstance(setup.resolver, TokenSourceAuthResolver)
        token_source = setup.resolver._token_source
        assert isinstance(token_source, ClientCredentialsTokenSource)
        assert token_source.audience_params == {'resource': 'https://api.example.com'}


def _resource_server_entry(**auth_overrides) -> ServerConfig:
    """Entry configured for the resource_server flow, with every requirement satisfied by default."""
    defaults = {
        'type': 'oauth2',
        'flow': 'resource_server',
        'issuer': 'https://auth.example.com',
        'audience': 'https://api.example.com',
        'client_id': 'gateway',
        'client_secret': 'secret',
    }
    return _entry(AuthConfig(**{**defaults, **auth_overrides}))


def _build_resource_server(entry: ServerConfig):
    """Run the handler with issuer discovery stubbed out."""
    metadata = IssuerMetadata(
        issuer='https://auth.example.com',
        jwks_uri='https://auth.example.com/jwks',
        token_endpoint='https://auth.example.com/token',
    )
    with (
        patch('openapi_mcp_gateway.auth.flows.resource_server.fetch_issuer_metadata', return_value=metadata),
        patch('openapi_mcp_gateway.auth.oidc._build_jwk_client'),
    ):
        return ResourceServerFlowHandler().build(
            OAuthFlowContext(
                entry=entry,
                spec=_build_spec(),
                oauth_flow=resolve_oauth_flow(entry, _build_spec()),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )
        )


class TestResourceServerFlowHandler:
    """The resource_server flow delegates issuance and exchanges the caller's token."""

    def test_advertises_the_gateway_as_the_resource_and_the_issuer_as_the_as(self):
        """The document names this endpoint's canonical URI, and the external issuer that mints for it.

        Advertising the upstream's identifier instead would make clients request a token
        the gateway must then refuse, since the MCP spec forbids accepting one minted for another resource.
        """
        setup = _build_resource_server(_resource_server_entry())

        assert setup.advertised_resource is not None
        assert setup.advertised_resource.resource == 'http://localhost:8000/srv/mcp'
        assert setup.advertised_resource.authorization_servers == ('https://auth.example.com',)

    def test_produces_a_verifier_and_no_provider(self):
        """The gateway validates rather than issues, which is what the SDK's two modes are."""
        setup = _build_resource_server(_resource_server_entry())

        assert setup.provider is None
        assert setup.verifier is not None
        assert isinstance(setup.resolver, TokenExchangeAuthResolver)

    def test_verifier_audience_is_the_gateway_not_the_upstream(self):
        """Inbound tokens must name this endpoint, even though the exchange targets the upstream."""
        setup = _build_resource_server(_resource_server_entry())

        assert setup.verifier.audience == 'http://localhost:8000/srv/mcp'

    def test_requires_issuer(self):
        """Without an issuer there is nothing to validate tokens against."""
        with pytest.raises(ValueError, match=r'requires auth\.issuer'):
            _build_resource_server(_resource_server_entry(issuer=None))

    def test_requires_an_upstream_audience(self):
        """Without a target the issuer mints for its own default, which the upstream refuses."""
        with pytest.raises(ValueError, match=r'requires auth\.resource or auth\.audience'):
            _build_resource_server(_resource_server_entry(audience=None))

    def test_requires_client_credentials_for_the_exchange(self):
        """Authorization servers require a confidential client for the token-exchange grant."""
        with pytest.raises(ValueError, match='requires client_id and client_secret'):
            _build_resource_server(_resource_server_entry(client_secret=None))

    def test_short_circuits_before_reading_the_spec(self):
        """``securitySchemes`` cannot declare this flow, so the detector is never consulted.

        Consulting it would raise a misleading "flow not declared" error on every spec.
        """
        entry = _resource_server_entry()

        assert resolve_oauth_flow(entry, _spec_with_authorization_code()).flow_type == 'resource_server'
