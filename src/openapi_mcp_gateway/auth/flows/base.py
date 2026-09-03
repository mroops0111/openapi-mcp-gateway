import abc
import dataclasses
import typing

from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata

from ...openapi import OpenAPISpec
from ...settings import ServerConfig
from ...stores.base import TokenStore
from ..detector import DetectedOAuthFlow
from ..resolver import AuthResolver


if typing.TYPE_CHECKING:
    from .authorization_code import AuthorizationCodeProvider


@dataclasses.dataclass
class OAuthFlowContext:
    """Inputs every ``OAuthFlowHandler`` needs to assemble its components.

    Populated by ``build_oauth_flow`` before it dispatches to a concrete handler.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    oauth_flow: DetectedOAuthFlow
    store: TokenStore
    gateway_url: str
    mount_path: str


@dataclasses.dataclass
class OAuthFlowSetup:
    """Result of an ``OAuthFlowHandler.build`` call, consumed by ``Gateway``.

    Only ``resolver`` is always set.
    ``provider`` is populated when the flow makes the gateway an MCP-side OAuth server,
    currently only ``authorization_code``.
    ``verifier`` is populated instead when the gateway validates tokens it did not issue.
    The two are mutually exclusive, and the MCP SDK rejects a server given both.
    ``settings`` and ``protected_resource`` accompany either one and drive the discovery documents.
    ``protected_resource`` is the RFC 9728 document this server publishes:
    its ``resource`` is always the endpoint's own canonical URI,
    while ``authorization_servers`` names whoever mints tokens for it.
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: AuthResolver
    provider: 'AuthorizationCodeProvider | None' = None
    settings: AuthSettings | None = None
    verifier: typing.Any | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None


class OAuthFlowHandler(abc.ABC):
    """Strategy describing how one OAuth flow wires upstream and MCP-side auth.

    Subclasses live under ``auth/flows/`` and register themselves in ``OAUTH_FLOW_HANDLERS``,
    so the factory can dispatch by ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        """Return the resolver, optional MCP provider and settings, and any shutdown hook."""
