import typing

from .assembly import AuthTypeContext, AuthTypeHandler, ServerAuthSetup
from .flows import build_oauth_flow
from .upstream.resolver import (
    UpstreamNoCredentialAuthResolver,
    UpstreamPassthroughAuthResolver,
    UpstreamStaticAuthResolver,
)


# Header formats live here rather than on the config.
# Knowing that a bearer token is written ``Bearer <token>`` is protocol knowledge, not configuration.
BEARER_PREFIX = 'Bearer '
DEFAULT_AUTHORIZATION_HEADER = 'Authorization'


class UpstreamNoCredentialHandler(AuthTypeHandler):
    """Send nothing upstream, for a public API."""

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        return ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver())


class UpstreamStaticCredentialHandler(AuthTypeHandler):
    """Send one fixed credential, captured from config at startup.

    Shared by ``bearer`` and ``api_key``, which differ only in header name and prefix.
    Every caller reaches the upstream as the same identity,
    so any per-user rule the upstream enforces stops distinguishing anyone.

    Falls back to sending nothing when no token is configured,
    which keeps a half-filled config serving a public API rather than failing at startup.
    """

    header_name: typing.ClassVar[str] = DEFAULT_AUTHORIZATION_HEADER
    value_prefix: typing.ClassVar[str] = ''

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        token = context.entry.auth.upstream.resolve_token()
        if not token:
            return ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver())
        return ServerAuthSetup(
            resolver=UpstreamStaticAuthResolver(
                header_value=f'{self.value_prefix}{token}',
                header_name=self.resolve_header_name(context),
            )
        )

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        """Header the credential travels in. Overridden where the config names it."""
        return self.header_name


class UpstreamBearerTokenHandler(UpstreamStaticCredentialHandler):
    """``Authorization: Bearer <token>``, the shape most token-based APIs expect."""

    value_prefix = BEARER_PREFIX


class UpstreamApiKeyHandler(UpstreamStaticCredentialHandler):
    """A raw key on whichever header the API names, defaulting to ``X-API-Key``."""

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        return context.entry.auth.upstream.api_key_header


class UpstreamPassthroughHandler(AuthTypeHandler):
    """Forward the caller's own ``Authorization`` header unchanged.

    No credential of the gateway's own, and no MCP-side check either.
    Only correct where the caller's token already addresses the upstream,
    which in practice means the in-process FastAPI integration.
    """

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        return ServerAuthSetup(resolver=UpstreamPassthroughAuthResolver())


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
