from .detector import DetectedOAuthFlow, detect_oauth_flows, detect_primary_oauth_flow
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
from .mcp.authorization_code import McpAuthorizationCodeServer
from .setup import AuthRoutes, AuthTypeContext, AuthTypeHandler, ServerAuthSetup
from .types import AUTH_TYPE_HANDLERS, build_auth
from .upstream.client_credentials import UpstreamClientCredentialsFlowHandler
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
    'DetectedOAuthFlow',
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
    'UpstreamStaticAuthResolver',
    'UpstreamTokenSource',
    'UpstreamTokenSourceAuthResolver',
    'build_auth',
    'build_oauth_flow',
    'detect_oauth_flows',
    'detect_primary_oauth_flow',
]
