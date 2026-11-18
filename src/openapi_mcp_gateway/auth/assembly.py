import abc
import dataclasses
import typing

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.extension import Extension
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..openapi import OpenAPISpec
from ..scopes import McpScopePolicy
from ..settings import ServerConfig
from ..stores.base import TokenStore
from .routes import AuthRoutes
from .upstream.oauth_flow import UpstreamOAuthFlow
from .upstream.resolver import UpstreamAuthResolver


class AuthTypeContext(typing.NamedTuple):
    """Everything an ``AuthTypeHandler`` may need, whether or not it uses all of it.

    Passing one shape to every handler is what keeps the caller free of per-type branching.
    Only ``oauth2`` reads the store, the URLs and ``mcp_scope_policy``, and that stays the handler's business.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    store: TokenStore
    gateway_url: str
    mcp_scope_policy: McpScopePolicy


@dataclasses.dataclass
class ServerAuthSetup:
    """What assembling one server's auth produces, for both directions, consumed by ``Gateway``.

    ``resolver`` is the upstream half and is always set.
    The rest is the MCP half, and stays empty where nothing checks who calls the endpoint.

    ``provider`` is set when the gateway is the authorization server for this endpoint.
    ``verifier`` is set instead when it validates tokens another server issued.
    The MCP SDK rejects a server given both.
    ``settings`` and ``protected_resource`` accompany either one and drive the discovery documents.
    ``protected_resource`` is the RFC 9728 document this server publishes.
    Its ``resource`` is always the endpoint's own canonical URI,
    while ``authorization_servers`` names whoever mints tokens for it.
    ``routes`` are mounted on the server as they are, so the gateway never needs to know which flow asked for them.
    ``mcp_tokens_carry_upstream_grant`` says each MCP token records what the upstream granted the user behind it,
    which checking an operation's upstream scopes needs.
    ``authorization_server_metadata`` is the RFC 8414 document of the gateway's own authorization server,
    set wherever the gateway issues this endpoint's tokens, whether through ``provider`` or its own routes.
    ``extensions`` are the MCP extensions the server advertises, such as how a client may obtain a token.
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: UpstreamAuthResolver
    provider: OAuthAuthorizationServerProvider[typing.Any, typing.Any, typing.Any] | None = None
    settings: AuthSettings | None = None
    verifier: TokenVerifier | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    routes: list[AuthRoutes] = dataclasses.field(default_factory=list)
    mcp_tokens_carry_upstream_grant: bool = False
    authorization_server_metadata: OAuthMetadata | None = None
    extensions: list[Extension] = dataclasses.field(default_factory=list)
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None
    # The flow that was resolved, which is not always the one the config named.
    # Set by the factory, so that every handler stays unaware of it.
    flow_type: str | None = None

    def __post_init__(self) -> None:
        if self.provider is not None and self.verifier is not None:
            raise ValueError('A server takes either an authorization server provider or a token verifier, not both.')


class AuthTypeHandler(abc.ABC):
    """Strategy that turns one ``auth.upstream.type`` into the components the gateway wires up."""

    @abc.abstractmethod
    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        """Return the resolver, and for OAuth also the provider or verifier and their settings."""


@dataclasses.dataclass
class OAuthFlowContext:
    """What an ``OAuthFlowHandler`` assembles from, which is what every auth type gets plus the resolved flow.

    Populated by ``build_oauth_flow`` before it dispatches to a concrete handler.
    ``upstream_oauth_flow`` is unset for ``token_exchange``, which runs no flow against the upstream.
    """

    auth_context: AuthTypeContext
    upstream_oauth_flow: UpstreamOAuthFlow | None = None

    @property
    def entry(self) -> ServerConfig:
        return self.auth_context.entry

    @property
    def spec(self) -> OpenAPISpec:
        return self.auth_context.spec

    @property
    def store(self) -> TokenStore:
        return self.auth_context.store

    @property
    def gateway_url(self) -> str:
        return self.auth_context.gateway_url

    @property
    def mcp_scope_policy(self) -> McpScopePolicy:
        return self.auth_context.mcp_scope_policy

    @property
    def mount_path(self) -> str:
        return self.auth_context.entry.mount_path

    def require_upstream_oauth_flow(self) -> UpstreamOAuthFlow:
        """The upstream OAuth flow, which every flow but ``token_exchange`` runs."""
        if self.upstream_oauth_flow is None:
            raise ValueError(
                f'Server "{self.entry.name}": this flow needs an upstream OAuth flow, and none was resolved.'
            )
        return self.upstream_oauth_flow


class OAuthFlowHandler(abc.ABC):
    """Strategy that assembles one ``auth.upstream.flow`` at startup.

    A flow whose two directions are independent builds only the upstream half.
    A flow that binds them builds both halves and the Bridge that joins them.
    Subclasses register in ``OAUTH_FLOW_HANDLERS``, so the factory can dispatch by ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
        """Return the upstream resolver and whatever MCP-side pieces and shutdown hook this flow brings."""
