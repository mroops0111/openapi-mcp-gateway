from .assembly import AuthRoutes, AuthTypeContext, AuthTypeHandler, ServerAuthSetup
from .factory import AUTH_TYPE_HANDLERS, build_auth
from .flows import (
    OAUTH_FLOW_HANDLERS,
    AuthorizationCodeBridge,
    AuthorizationCodeFlowHandler,
    OAuthFlowContext,
    OAuthFlowHandler,
    TokenExchangeBridge,
    TokenExchangeFlowHandler,
    build_oauth_flow,
)
from .flows.client_credentials import UpstreamClientCredentialsFlowHandler
from .mcp.authorization_code import McpAuthorizationCodeServer
from .upstream.oauth_flow import UpstreamOAuthFlow, detect_upstream_oauth_flows
from .upstream.resolver import (
    UpstreamAuthResolver,
    UpstreamNoCredentialAuthResolver,
    UpstreamStaticAuthResolver,
    UpstreamTokenSourceAuthResolver,
)
from .upstream.token_source import UpstreamClientCredentialsTokenSource, UpstreamTokenSource


__all__ = [
    'AUTH_TYPE_HANDLERS',
    'OAUTH_FLOW_HANDLERS',
    'AuthRoutes',
    'AuthTypeContext',
    'AuthTypeHandler',
    'AuthorizationCodeBridge',
    'AuthorizationCodeFlowHandler',
    'McpAuthorizationCodeServer',
    'OAuthFlowContext',
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
    'build_oauth_flow',
    'detect_upstream_oauth_flows',
]
