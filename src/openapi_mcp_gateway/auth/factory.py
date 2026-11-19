import dataclasses
import typing

from .assembly import AuthTypeContext, AuthTypeHandler, ServerAuthSetup
from .flows import OAUTH_FLOW_HANDLERS, build_oauth_flow
from .mcp_authority import MCP_TOKEN_AUTHORITIES, attach_mcp_token_authority
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

    upstream_principal = 'gateway'

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

    upstream_principal = 'gateway'
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

    upstream_principal = 'caller'

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        return ServerAuthSetup(resolver=UpstreamPassthroughAuthResolver())


class OAuth2Handler(AuthTypeHandler):
    """Defer to ``auth.upstream.flow``, which selects among the OAuth grants in turn and decides the principal."""

    upstream_principal = None

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
    """Assemble the auth components for one server, dispatching on ``auth.upstream.type``.

    An upstream strategy that leaves the MCP half open gets one from ``MCP_TOKEN_AUTHORITIES``.
    An ``auth.mcp`` field the resulting MCP half never reads is refused.
    """
    auth_type = context.entry.auth.upstream.type
    handler_class = AUTH_TYPE_HANDLERS.get(auth_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{context.entry.name}": unsupported auth type "{auth_type}". '
            f'Supported types: {sorted(AUTH_TYPE_HANDLERS)}'
        )
    setup = handler_class().build(context)
    # ``oauth2`` leaves the principal to its flow, whose factory has already set it.
    setup = dataclasses.replace(setup, upstream_principal=handler_class.upstream_principal or setup.upstream_principal)
    setup = attach_mcp_token_authority(setup, context)
    _refuse_unread_mcp_settings(setup, context)
    return setup


def _refuse_unread_mcp_settings(setup: ServerAuthSetup, context: AuthTypeContext) -> None:
    """Refuse an ``auth.mcp`` field the MCP half never reads, naming what would read it.

    Only explicitly-set fields are checked, so defaults never trip this.
    The suggestion prefers strategies for an upstream that sees the same identity, since those need no other change.
    """
    entry = context.entry
    unread = sorted(entry.auth.mcp.model_fields_set - setup.mcp_settings_read)
    if not unread:
        return
    field = unread[0]
    readers = [
        reader for reader in (*OAUTH_FLOW_HANDLERS.values(), *MCP_TOKEN_AUTHORITIES) if field in reader.mcp_settings
    ]
    nearest = [reader for reader in readers if reader.upstream_principal == setup.upstream_principal] or readers
    requirement = ', or '.join(reader.mcp_settings_requirement for reader in nearest)
    raise ValueError(f'Server "{entry.name}": auth.mcp.{field} needs {requirement}, so it would have no effect here.')
