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


@dataclasses.dataclass(frozen=True)
class AdvertisedResource:
    """What the gateway's RFC 9728 document says about one server.

    ``resource`` is always this MCP endpoint's own canonical URI,
    since a client must request its token for the server it is calling.
    ``authorization_servers`` names whoever mints those tokens,
    which is the gateway itself under ``authorization_code`` and an external issuer under ``token_exchange``.
    """

    resource: str
    authorization_servers: tuple[str, ...]
    scopes_supported: tuple[str, ...] = ()


@dataclasses.dataclass
class OAuthFlowSetup:
    """Result of an ``OAuthFlowHandler.build`` call, consumed by ``Gateway``.

    Only ``resolver`` is always set.
    ``provider`` is populated when the flow makes the gateway an MCP-side OAuth server,
    currently only ``authorization_code``.
    ``verifier`` is populated instead when the gateway validates tokens it did not issue.
    The two are mutually exclusive, and the MCP SDK rejects a server given both.
    ``settings`` and ``advertised_resource`` accompany either one and drive the discovery documents.
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: AuthResolver
    provider: 'AuthorizationCodeProvider | None' = None
    settings: AuthSettings | None = None
    verifier: typing.Any | None = None
    advertised_resource: AdvertisedResource | None = None
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None


class OAuthFlowHandler(abc.ABC):
    """Strategy describing how one OAuth flow wires upstream and MCP-side auth.

    Subclasses live under ``auth/flows/`` and register themselves in ``OAUTH_FLOW_HANDLERS``,
    so the factory can dispatch by ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        """Return the resolver, optional MCP provider and settings, and any shutdown hook."""
