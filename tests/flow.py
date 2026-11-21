from openapi_mcp_gateway.auth.assembly import AuthTypeContext
from openapi_mcp_gateway.openapi import OpenAPISpec
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy, UpstreamFixedScopeRequest
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, AUTHORIZE_URL, TOKEN_URL


def build_spec(security_schemes: dict | None = None) -> OpenAPISpec:
    """Construct a minimal ``OpenAPISpec`` carrying the given ``securitySchemes``."""
    return OpenAPISpec(
        title='test',
        version='1.0.0',
        operations=[],
        security_schemes=security_schemes or {},
    )


def spec_with_authorization_code() -> OpenAPISpec:
    """Spec declaring only the ``authorization_code`` flow."""
    return build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': AUTHORIZE_URL,
                        'tokenUrl': TOKEN_URL,
                        'scopes': {'read': 'r', 'write': 'w'},
                    },
                },
            },
        }
    )


def spec_with_client_credentials() -> OpenAPISpec:
    """Spec declaring only the ``client_credentials`` flow."""
    return build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'clientCredentials': {
                        'tokenUrl': TOKEN_URL,
                        'scopes': {'api': 'api'},
                    },
                },
            },
        }
    )


def spec_with_both_flows() -> OpenAPISpec:
    """Spec declaring both ``authorization_code`` and ``client_credentials``."""
    return build_spec(
        {
            'oauth': {
                'type': 'oauth2',
                'flows': {
                    'authorizationCode': {
                        'authorizationUrl': AUTHORIZE_URL,
                        'tokenUrl': TOKEN_URL,
                        'scopes': {'api': 'api'},
                    },
                    'clientCredentials': {
                        'tokenUrl': TOKEN_URL,
                        'scopes': {'api': 'api'},
                    },
                },
            },
        }
    )


def auth_context(entry: ServerConfig, spec: OpenAPISpec) -> AuthTypeContext:
    """What ``build_auth`` hands a handler for ``entry``, on a fresh store and with no MCP scopes."""
    return AuthTypeContext(
        entry=entry,
        spec=spec,
        store=MemoryTokenStore(),
        gateway_url='http://localhost:8000',
        mcp_scope_policy=McpUnrestrictedScopePolicy(),
        upstream_scope_request=UpstreamFixedScopeRequest(entry.auth.upstream.scopes),
        client_metadata_resolver=serving_resolver({}),
    )


def server_entry(auth: AuthConfig, name: str = 'srv') -> ServerConfig:
    """Minimal ``ServerConfig`` carrying ``auth`` for factory tests."""
    return ServerConfig(name=name, spec='dummy.json', base_url=API_URL, auth=auth)
