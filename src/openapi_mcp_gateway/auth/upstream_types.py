import abc
import dataclasses
import typing

from .assembly import (
    AuthTypeContext,
    McpSettingsReader,
    ServerAuthSetup,
    UpstreamDescription,
    UpstreamSelection,
    UpstreamSetting,
)
from .flows import OAuthFlowSelection, select_oauth_flow
from .upstream.resolver import (
    UpstreamNoCredentialAuthResolver,
    UpstreamPassthroughAuthResolver,
    UpstreamStaticAuthResolver,
)


# Header formats live here rather than on the config.
# Knowing that a bearer token is written ``Bearer <token>`` is protocol knowledge, not configuration.
BEARER_PREFIX = 'Bearer '
DEFAULT_AUTHORIZATION_HEADER = 'Authorization'


# What one ``AuthTypeHandler`` settles in ``select``, handed back to its ``build``.
SelectionT = typing.TypeVar('SelectionT', bound=UpstreamSelection)


class AuthTypeHandler(abc.ABC, typing.Generic[SelectionT]):
    """Strategy that turns one ``auth.upstream.type`` into the components the gateway wires up.

    ``build`` receives what ``select`` settled, so nothing is resolved twice.
    """

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        """The ``auth.upstream`` fields this type reads whatever else is selected. None, by default."""
        return frozenset()

    @abc.abstractmethod
    def select(self, context: AuthTypeContext) -> SelectionT:
        """Settle what this handler would build, without any I/O."""

    @abc.abstractmethod
    def build(self, context: AuthTypeContext, selection: SelectionT) -> ServerAuthSetup:
        """Return the resolver, and for OAuth also the provider or verifier and their settings."""


class UpstreamNoCredentialHandler(AuthTypeHandler[UpstreamSelection]):
    """Send nothing upstream, for a public API."""

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='gateway')

    def build(self, context: AuthTypeContext, selection: UpstreamSelection) -> ServerAuthSetup:
        return ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver())


class UpstreamStaticCredentialHandler(AuthTypeHandler[UpstreamSelection]):
    """Send one fixed credential, captured from config at startup.

    Shared by ``bearer`` and ``api_key``, which differ only in header name and prefix.
    Every caller reaches the upstream as the same identity,
    so any per-user rule the upstream enforces stops distinguishing anyone.

    Falls back to sending nothing when no token is configured,
    which keeps a half-filled config serving a public API rather than failing at startup.
    """

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset({'token'})

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='gateway', upstream_settings=self.upstream_settings)

    def build(self, context: AuthTypeContext, selection: UpstreamSelection) -> ServerAuthSetup:
        token = context.entry.auth.upstream.resolve_token()
        resolver = (
            UpstreamStaticAuthResolver(
                header_value=f'{self.value_prefix}{token}', header_name=self.resolve_header_name(context)
            )
            if token
            else UpstreamNoCredentialAuthResolver()
        )
        return ServerAuthSetup(
            resolver=resolver,
            upstream_description=UpstreamDescription(self.describe(context), self.resolve_settings(context)),
        )

    @abc.abstractmethod
    def describe(self, context: AuthTypeContext) -> str:
        """How the upstream is reached, as ``mechanism (detail)``."""

    @property
    def value_prefix(self) -> str:
        """What precedes the credential in its header, such as ``Bearer``. Nothing, by default."""
        return ''

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        """Header the credential travels in. Overridden where the config names it."""
        return DEFAULT_AUTHORIZATION_HEADER

    def resolve_settings(self, context: AuthTypeContext) -> dict[UpstreamSetting, str]:
        """The ``auth.upstream`` fields this credential settles, as ``UpstreamDescription`` reports them. None, by default."""
        return {}


class UpstreamBearerTokenHandler(UpstreamStaticCredentialHandler):
    """``Authorization: Bearer <token>``, the shape most token-based APIs expect."""

    @property
    def value_prefix(self) -> str:
        return BEARER_PREFIX

    def describe(self, context: AuthTypeContext) -> str:
        return 'bearer'


class UpstreamApiKeyHandler(UpstreamStaticCredentialHandler):
    """A raw key on whichever header the API names, defaulting to ``X-API-Key``."""

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset({'token', 'api_key_header'})

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        return context.entry.auth.upstream.api_key_header

    def describe(self, context: AuthTypeContext) -> str:
        return f'api_key ({self.resolve_header_name(context)})'

    def resolve_settings(self, context: AuthTypeContext) -> dict[UpstreamSetting, str]:
        return {'api_key_header': self.resolve_header_name(context)}


class UpstreamPassthroughHandler(AuthTypeHandler[UpstreamSelection]):
    """Forward the caller's own ``Authorization`` header unchanged.

    No credential of the gateway's own, and no MCP-side check either.
    Only correct where the caller's token already addresses the upstream,
    which in practice means the in-process FastAPI integration.
    """

    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        return UpstreamSelection(upstream_principal='caller')

    def build(self, context: AuthTypeContext, selection: UpstreamSelection) -> ServerAuthSetup:
        return ServerAuthSetup(
            resolver=UpstreamPassthroughAuthResolver(), upstream_description=UpstreamDescription('passthrough')
        )


@dataclasses.dataclass(frozen=True)
class OAuthUpstreamSelection(UpstreamSelection):
    """What ``UpstreamOAuth2Handler`` settles, carrying the flow it resolved so building does not resolve it again."""

    flow_selection: OAuthFlowSelection = dataclasses.field(kw_only=True)


class UpstreamOAuth2Handler(AuthTypeHandler[OAuthUpstreamSelection]):
    """Defer to ``auth.upstream.flow``, which selects among the OAuth grants in turn and decides the principal."""

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset({'flow'})

    def select(self, context: AuthTypeContext) -> OAuthUpstreamSelection:
        flow_selection = select_oauth_flow(context)
        handler = flow_selection.handler
        return OAuthUpstreamSelection(
            upstream_principal=handler.upstream_principal,
            upstream_settings=self.upstream_settings | handler.upstream_settings,
            mcp_settings_reader=handler if isinstance(handler, McpSettingsReader) else None,
            flow_selection=flow_selection,
        )

    def build(self, context: AuthTypeContext, selection: OAuthUpstreamSelection) -> ServerAuthSetup:
        setup = selection.flow_selection.build(context)
        # The flow that was resolved, which is not always the one the config named.
        flow_type = selection.flow_selection.flow_type
        return dataclasses.replace(
            setup, upstream_description=UpstreamDescription(f'oauth2 ({flow_type})', {'flow': flow_type})
        )


# The extension point for ``auth.upstream.type``, mirrored one level down by ``OAUTH_FLOW_HANDLERS`` for the flow.
# Adding a type means adding a handler and one entry here, rather than a branch every other type passes through.
# It follows the classes it names, since Python builds it at import time.
AUTH_TYPE_HANDLERS: dict[str, type[AuthTypeHandler[typing.Any]]] = {
    'none': UpstreamNoCredentialHandler,
    'bearer': UpstreamBearerTokenHandler,
    'api_key': UpstreamApiKeyHandler,
    'passthrough': UpstreamPassthroughHandler,
    'oauth2': UpstreamOAuth2Handler,
}
