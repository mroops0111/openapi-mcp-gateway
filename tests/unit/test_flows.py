import pytest

from openapi_mcp_gateway.auth.flows import (
    AuthorizationCodeFlowHandler,
    ClientCredentialsFlowHandler,
    OAuthFlowContext,
    build_oauth_flow,
)
from openapi_mcp_gateway.auth.flows.factory import resolve_oauth_flow
from openapi_mcp_gateway.auth.resolver import AuthorizationCodeAuthResolver, TokenSourceAuthResolver
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

    def test_authorization_code_requires_credentials(self):
        """Missing client_id/client_secret on authorization_code raises ``ValueError``."""
        entry = _entry(AuthConfig(type='oauth2'))
        with pytest.raises(ValueError, match='client_id and client_secret'):
            build_oauth_flow(
                entry=entry,
                spec=_spec_with_authorization_code(),
                store=MemoryTokenStore(),
                gateway_url='http://localhost:8000',
                mount_path='/srv',
            )


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
