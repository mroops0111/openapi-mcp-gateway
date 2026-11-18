from .flows import build_oauth_flow
from .setup import AuthTypeContext, AuthTypeHandler, ServerAuthSetup
from .upstream.handler import (
    UpstreamApiKeyHandler,
    UpstreamBearerTokenHandler,
    UpstreamNoCredentialHandler,
    UpstreamPassthroughHandler,
)


class OAuth2Handler(AuthTypeHandler):
    """Defer to ``auth.upstream.flow``, which selects among the OAuth grants in turn."""

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        return build_oauth_flow(context)


# The extension point for ``auth.upstream.type``, mirrored one level down by ``OAUTH_FLOW_HANDLERS`` for the flow.
# Adding a type means adding a handler and one entry here, rather than a branch every other type passes through.
# It follows the classes it names, since Python builds it at import time.
AUTH_TYPE_HANDLERS: dict[str, type[AuthTypeHandler]] = {
    'none': UpstreamNoCredentialHandler,
    'bearer': UpstreamBearerTokenHandler,
    'api_key': UpstreamApiKeyHandler,
    'passthrough': UpstreamPassthroughHandler,
    'oauth2': OAuth2Handler,
}


def build_auth(context: AuthTypeContext) -> ServerAuthSetup:
    """Assemble the auth components for one server, dispatching on ``auth.upstream.type``."""
    auth_type = context.entry.auth.upstream.type
    handler_class = AUTH_TYPE_HANDLERS.get(auth_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{context.entry.name}": unsupported auth type "{auth_type}". '
            f'Supported types: {sorted(AUTH_TYPE_HANDLERS)}'
        )
    return handler_class().build(context)
