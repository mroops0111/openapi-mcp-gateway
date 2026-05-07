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

    Fields are filled in by the factory (``build_oauth_flow``) before
    dispatching to a concrete handler.
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

    Only ``resolver`` is always present. ``provider`` and ``settings`` are
    populated when a flow needs to act as an MCP-side OAuth server (currently
    only ``authorization_code``). ``on_shutdown`` lets a flow register a
    cleanup callback that the gateway invokes when shutting down.
    """

    resolver: AuthResolver
    provider: 'AuthorizationCodeProvider | None' = None
    settings: AuthSettings | None = None
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None


class OAuthFlowHandler(abc.ABC):
    """Strategy: how a single OAuth flow type wires upstream + MCP-side auth.

    Subclasses live under ``auth/flows/`` and are registered in
    ``OAUTH_FLOW_HANDLERS`` so the factory can dispatch to them by
    ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        """Return the resolver, optional MCP provider/settings, and shutdown hook."""
