import abc
import dataclasses

from ...openapi import OpenAPISpec
from ...scopes import McpScopePolicy
from ...settings import ServerConfig
from ...stores.base import TokenStore
from ..detector import DetectedOAuthFlow
from ..setup import ServerAuthSetup


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
    mcp_scope_policy: McpScopePolicy


class OAuthFlowHandler(abc.ABC):
    """Strategy that assembles one ``auth.upstream.flow`` at startup.

    A flow whose two directions are independent builds only the upstream half.
    A flow that binds them builds both halves and the Bridge that joins them.
    Subclasses register in ``OAUTH_FLOW_HANDLERS``, so the factory can dispatch by ``flow_type``.
    """

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
        """Return the upstream resolver and whatever MCP-side pieces and shutdown hook this flow brings."""
