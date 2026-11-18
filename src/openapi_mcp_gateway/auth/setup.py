import abc
import dataclasses
import typing

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata

from ..openapi import OpenAPISpec
from ..scopes import McpScopePolicy
from ..settings import ServerConfig
from ..stores.base import TokenStore
from .upstream.resolver import UpstreamAuthResolver


if typing.TYPE_CHECKING:
    from .mcp.authorization_code import McpAuthorizationCodeServer


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


class AuthRoutes(abc.ABC):
    """Browser-facing routes one server's auth needs beside the MCP SDK's own, such as a consent page."""

    @abc.abstractmethod
    def register(self, mcp: MCPServer) -> None:
        """Mount the routes on ``mcp``, under the server's own path."""


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
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: UpstreamAuthResolver
    provider: 'McpAuthorizationCodeServer | None' = None
    settings: AuthSettings | None = None
    verifier: TokenVerifier | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    routes: list[AuthRoutes] = dataclasses.field(default_factory=list)
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None
    # The flow that was resolved, which is not always the one the config named.
    # Set by the factory, so that every handler stays unaware of it.
    flow_type: str | None = None

    def __post_init__(self) -> None:
        if self.provider is not None and self.verifier is not None:
            raise ValueError('A server takes either an authorization server provider or a token verifier, not both.')

    def has_mcp_auth(self) -> bool:
        """Whether a caller of this MCP endpoint must present a token."""
        return self.provider is not None or self.verifier is not None


class AuthTypeHandler(abc.ABC):
    """Strategy that turns one ``auth.upstream.type`` into the components the gateway wires up."""

    @abc.abstractmethod
    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        """Return the resolver, and for OAuth also the provider or verifier and their settings."""
