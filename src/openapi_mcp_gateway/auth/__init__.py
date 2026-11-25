from .assembly import AuthRoutes, AuthTypeContext, ServerAuthSetup
from .factory import build_auth
from .flows import (
    OAUTH_FLOW_HANDLERS,
    AuthorizationCodeBridge,
    AuthorizationCodeFlowHandler,
    OAuthFlowHandler,
    TokenExchangeBridge,
    TokenExchangeFlowHandler,
)
from .flows.client_credentials import UpstreamClientCredentialsFlowHandler
from .mcp.authorization_code import McpAuthorizationCodeServer
from .upstream.client_credentials import UpstreamClientCredentialsTokenSource
from .upstream.oauth_flow import UpstreamOAuthFlow, detect_upstream_oauth_flows
from .upstream.resolver import (
    UpstreamAuthResolver,
    UpstreamNoCredentialAuthResolver,
    UpstreamStaticAuthResolver,
    UpstreamTokenSourceAuthResolver,
)
from .upstream.token_source import UpstreamTokenSource
from .upstream_types import AUTH_TYPE_HANDLERS, AuthTypeHandler


__all__ = [
    'AUTH_TYPE_HANDLERS',
    'OAUTH_FLOW_HANDLERS',
    'AuthRoutes',
    'AuthTypeContext',
    'AuthTypeHandler',
    'AuthorizationCodeBridge',
    'AuthorizationCodeFlowHandler',
    'McpAuthorizationCodeServer',
    'OAuthFlowHandler',
    'ServerAuthSetup',
    'TokenExchangeBridge',
    'TokenExchangeFlowHandler',
    'UpstreamAuthResolver',
    'UpstreamClientCredentialsFlowHandler',
    'UpstreamClientCredentialsTokenSource',
    'UpstreamNoCredentialAuthResolver',
    'UpstreamOAuthFlow',
    'UpstreamStaticAuthResolver',
    'UpstreamTokenSource',
    'UpstreamTokenSourceAuthResolver',
    'build_auth',
    'detect_upstream_oauth_flows',
]
