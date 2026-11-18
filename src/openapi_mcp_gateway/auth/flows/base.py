import abc
import dataclasses

from ...openapi import OpenAPISpec
from ...scopes import McpScopePolicy
from ...settings import ServerConfig
from ...stores.base import TokenStore
from ..setup import ServerAuthSetup
from ..upstream.oauth_flow import UpstreamOAuthFlow


@dataclasses.dataclass
class OAuthFlowContext:
    """Inputs every ``OAuthFlowHandler`` needs to assemble its components.

    Populated by ``build_oauth_flow`` before it dispatches to a concrete handler.
    ``upstream_oauth_flow`` is unset for ``token_exchange``, which runs no flow against the upstream.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    store: TokenStore
    gateway_url: str
    mount_path: str
    mcp_scope_policy: McpScopePolicy
    upstream_oauth_flow: UpstreamOAuthFlow | None = None

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
