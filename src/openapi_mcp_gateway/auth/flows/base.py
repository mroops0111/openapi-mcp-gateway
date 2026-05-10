import abc
import dataclasses
import typing

from mcp.server.auth.settings import AuthSettings

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

    Only ``resolver`` is always set. ``provider`` and ``settings`` are populated
    when the flow needs to act as an MCP-side OAuth server (currently only
    ``authorization_code``).
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: AuthResolver
    provider: 'AuthorizationCodeProvider | None' = None
    settings: AuthSettings | None = None
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None


class OAuthFlowHandler(abc.ABC):
    """Strategy describing how one OAuth flow wires upstream and MCP-side auth.

    Subclasses live under ``auth/flows/`` and register themselves in
    ``OAUTH_FLOW_HANDLERS`` so the factory can dispatch by ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        """Return the resolver, optional MCP provider and settings, and any shutdown hook."""
