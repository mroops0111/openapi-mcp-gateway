from .assembly import (
    AuthTypeContext,
    AuthTypeHandler,
    McpSettingsReader,
    ServerAuthSetup,
    UpstreamPrincipal,
    UpstreamSelection,
)
from .authorities import MCP_TOKEN_AUTHORITIES, select_mcp_token_authority
from .flows import OAUTH_FLOW_HANDLERS, build_oauth_flow, select_oauth_flow
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

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='gateway')

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

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='gateway')

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

    @property
    def value_prefix(self) -> str:
        """What precedes the credential in its header, such as ``Bearer``. Nothing, by default."""
        return ''

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        """Header the credential travels in. Overridden where the config names it."""
        return DEFAULT_AUTHORIZATION_HEADER


class UpstreamBearerTokenHandler(UpstreamStaticCredentialHandler):
    """``Authorization: Bearer <token>``, the shape most token-based APIs expect."""

    @property
    def value_prefix(self) -> str:
        return BEARER_PREFIX


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

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='caller')

    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        return ServerAuthSetup(resolver=UpstreamPassthroughAuthResolver())


class OAuth2Handler(AuthTypeHandler):
    """Defer to ``auth.upstream.flow``, which selects among the OAuth grants in turn and decides the principal."""

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        handler = select_oauth_flow(context).handler_class()
        return UpstreamSelection(
            upstream_principal=handler.upstream_principal,
            mcp_settings_reader=handler if isinstance(handler, McpSettingsReader) else None,
        )

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

    Every strategy is selected first without any I/O, so a setting nothing reads is refused before anything is built.
    An upstream strategy that leaves the MCP half open gets one from ``MCP_TOKEN_AUTHORITIES``.
    """
    entry = context.entry
    auth_type = entry.auth.upstream.type
    handler_class = AUTH_TYPE_HANDLERS.get(auth_type)
    if handler_class is None:
        raise ValueError(
            f'Server "{entry.name}": unsupported auth type "{auth_type}". Supported types: {sorted(AUTH_TYPE_HANDLERS)}'
        )
    handler = handler_class()
    selection = handler.select(context)
    authority = (
        None if selection.mcp_settings_reader else select_mcp_token_authority(entry.auth, selection.upstream_principal)
    )
    reader = selection.mcp_settings_reader or (authority if isinstance(authority, McpSettingsReader) else None)
    mcp_settings_read = reader.mcp_settings if reader else frozenset()
    _refuse_unread_mcp_settings(
        entry.name, entry.auth.mcp.model_fields_set - mcp_settings_read, selection.upstream_principal
    )
    if reader is not None:
        reader.refuse_unusable_settings(entry.auth, entry.name)

    setup = handler.build(context)
    return authority.attach(setup, context) if authority else setup


def _refuse_unread_mcp_settings(server_name: str, unread: set[str], upstream_principal: UpstreamPrincipal) -> None:
    """Refuse an ``auth.mcp`` field the MCP half would never read, naming what would read it.

    Only explicitly-set fields are checked, so defaults never trip this.
    The suggestion prefers strategies for an upstream that sees the same identity, since those need no other change.
    """
    if not unread:
        return
    field = sorted(unread)[0]
    strategies = [
        *(handler_class() for handler_class in OAUTH_FLOW_HANDLERS.values()),
        *(authority_class() for authority_class in MCP_TOKEN_AUTHORITIES),
    ]
    readers = [
        strategy
        for strategy in strategies
        if isinstance(strategy, McpSettingsReader) and field in strategy.mcp_settings
    ]
    nearest = [reader for reader in readers if reader.upstream_principal == upstream_principal] or readers
    requirement = ', or '.join(reader.mcp_settings_requirement for reader in nearest)
    raise ValueError(f'Server "{server_name}": auth.mcp.{field} needs {requirement}, so it would have no effect here.')
