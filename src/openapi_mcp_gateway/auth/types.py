"""Strategies for assembling auth components from ``auth.type``.

``auth.type`` says where the upstream credential comes from, and each answer needs different
machinery. A static credential is captured once at startup, ``oauth2`` obtains one per flow,
``passthrough`` forwards the caller's, and ``none`` sends nothing.

This mirrors ``OAuthFlowHandler``, which does the same job one level down for ``auth.flow``.
Both exist so that adding a variant means adding a class and a registry entry,
rather than editing a branch that every other variant also passes through.

Header formats live here rather than on ``AuthConfig``, since knowing that a bearer token is
written ``Bearer <token>`` is protocol knowledge rather than configuration.
"""

import abc
import typing

from ..openapi import OpenAPISpec
from ..settings import ServerConfig
from ..stores.base import TokenStore
from .flows import OAuthFlowSetup, build_oauth_flow
from .resolver import NullAuthResolver, PassthroughAuthResolver, StaticAuthResolver


BEARER_PREFIX = 'Bearer '
DEFAULT_AUTHORIZATION_HEADER = 'Authorization'


class AuthTypeContext(typing.NamedTuple):
    """Everything an ``AuthTypeHandler`` may need, whether or not it uses all of it.

    Passing one shape to every handler is what keeps the caller free of per-type branching.
    Only ``oauth2`` reads the store and the URLs, and that stays the handler's business.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    store: TokenStore
    gateway_url: str


class AuthTypeHandler(abc.ABC):
    """Strategy that turns one ``auth.type`` into the components the gateway wires up."""

    @abc.abstractmethod
    def build(self, context: AuthTypeContext) -> OAuthFlowSetup:
        """Return the resolver, and for OAuth also the provider or verifier and their settings."""


class NoAuthHandler(AuthTypeHandler):
    """Send nothing upstream, for a public API."""

    def build(self, context: AuthTypeContext) -> OAuthFlowSetup:
        return OAuthFlowSetup(resolver=NullAuthResolver())


class StaticCredentialHandler(AuthTypeHandler):
    """Send one fixed credential, captured from config at startup.

    Shared by ``bearer`` and ``api_key``, which differ only in header name and prefix.
    Every caller reaches the upstream as the same identity, so any per-user rule the upstream
    enforces stops distinguishing anyone.

    Falls back to sending nothing when no token is configured,
    which keeps a half-filled config serving a public API rather than failing at startup.
    """

    header_name: typing.ClassVar[str] = DEFAULT_AUTHORIZATION_HEADER
    value_prefix: typing.ClassVar[str] = ''

    def build(self, context: AuthTypeContext) -> OAuthFlowSetup:
        token = context.entry.auth.resolve_token()
        if not token:
            return OAuthFlowSetup(resolver=NullAuthResolver())
        return OAuthFlowSetup(
            resolver=StaticAuthResolver(
                header_value=f'{self.value_prefix}{token}',
                header_name=self.resolve_header_name(context),
            )
        )

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        """Header the credential travels in. Overridden where the config names it."""
        return self.header_name


class BearerTokenHandler(StaticCredentialHandler):
    """``Authorization: Bearer <token>``, the shape most token-based APIs expect."""

    value_prefix = BEARER_PREFIX


class ApiKeyHandler(StaticCredentialHandler):
    """A raw key on whichever header the API names, defaulting to ``X-API-Key``."""

    def resolve_header_name(self, context: AuthTypeContext) -> str:
        return context.entry.auth.api_key_header


class PassthroughHandler(AuthTypeHandler):
    """Forward the caller's own ``Authorization`` header unchanged.

    No credential of the gateway's own, and no MCP-side check either.
    Only correct where the caller's token already addresses the upstream,
    which in practice means the in-process FastAPI integration.
    """

    def build(self, context: AuthTypeContext) -> OAuthFlowSetup:
        return OAuthFlowSetup(resolver=PassthroughAuthResolver())


class OAuth2Handler(AuthTypeHandler):
    """Defer to ``auth.flow``, which selects among the OAuth grants in turn."""

    def build(self, context: AuthTypeContext) -> OAuthFlowSetup:
        return build_oauth_flow(
            entry=context.entry,
            spec=context.spec,
            store=context.store,
            gateway_url=context.gateway_url,
            mount_path=context.entry.mount_path,
        )


# The extension point. Adding a type means adding a handler above and one entry here,
# and it necessarily follows the classes it names.
AUTH_TYPE_HANDLERS: dict[str, type[AuthTypeHandler]] = {
    'none': NoAuthHandler,
    'bearer': BearerTokenHandler,
    'api_key': ApiKeyHandler,
    'passthrough': PassthroughHandler,
    'oauth2': OAuth2Handler,
}


def build_auth(context: AuthTypeContext) -> OAuthFlowSetup:
    """Assemble the auth components for one server, dispatching on ``auth.type``."""
    handler_class = AUTH_TYPE_HANDLERS.get(context.entry.auth.type)
    if handler_class is None:
        raise ValueError(
            f'Server "{context.entry.name}": unsupported auth type "{context.entry.auth.type}". '
            f'Supported types: {sorted(AUTH_TYPE_HANDLERS)}'
        )
    return handler_class().build(context)
