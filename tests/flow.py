import contextlib
import typing
from unittest.mock import MagicMock, patch

from openapi_mcp_gateway.auth.assembly import AuthTypeContext, ServerAuthSetup
from openapi_mcp_gateway.auth.flows import select_oauth_flow
from openapi_mcp_gateway.auth.mcp.external_issuer import McpExternalIssuerMetadata
from openapi_mcp_gateway.openapi import OpenAPISpec
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy, ScopeRegistry
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, AUTHORIZE_URL, ISSUER, JWKS_URL, TOKEN_URL


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
        scope_registry=ScopeRegistry(McpUnrestrictedScopePolicy()),
        client_metadata_resolver=serving_resolver({}),
    )


def server_entry(auth: AuthConfig, name: str = 'srv') -> ServerConfig:
    """Minimal ``ServerConfig`` carrying ``auth`` for factory tests."""
    return ServerConfig(name=name, spec='dummy.json', base_url=API_URL, auth=auth)


def build_selected_oauth_flow(context: AuthTypeContext) -> ServerAuthSetup:
    """The OAuth flow ``context`` selects, built as ``UpstreamOAuth2Handler`` builds it."""
    return select_oauth_flow(context).build(context)


@contextlib.contextmanager
def discovered_external_issuer(jwk_client: MagicMock | None = None) -> typing.Iterator[MagicMock]:
    """Stub out discovery of ``ISSUER`` and its signing keys, so a server it mints tokens for builds offline.

    Yields the stubbed metadata fetch, which answers with ``ISSUER``'s documents.
    """
    metadata = McpExternalIssuerMetadata(issuer=ISSUER, jwks_uri=JWKS_URL, token_endpoint=TOKEN_URL)
    with (
        patch(
            'openapi_mcp_gateway.auth.external_issuer_setup.fetch_mcp_external_issuer_metadata', return_value=metadata
        ) as fetch,
        patch('openapi_mcp_gateway.auth.mcp.external_issuer._build_jwk_client', return_value=jwk_client or MagicMock()),
    ):
        yield fetch
