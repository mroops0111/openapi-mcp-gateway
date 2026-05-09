import logging

from ..resolver import PassthroughAuthResolver
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


class PassthroughFlowHandler(OAuthFlowHandler):
    """Forward the MCP client's ``Authorization`` header to the upstream API.

    No MCP-side OAuth server, no token store, no upstream credential exchange.
    Suitable when the gateway and the upstream API share an OAuth realm so the
    MCP client's existing token is already accepted upstream — typically the
    case for FastAPI integrations where the gateway is mounted onto the same
    app it exposes.
    """

    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        """Build a setup whose only component is a ``PassthroughAuthResolver``."""
        logger.debug(
            'Passthrough flow set up for "%s": MCP client Authorization forwarded verbatim',
            flow_context.entry.name,
        )
        return OAuthFlowSetup(resolver=PassthroughAuthResolver())
