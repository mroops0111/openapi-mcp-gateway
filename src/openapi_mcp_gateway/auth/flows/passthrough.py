import logging

from ..resolver import PassthroughAuthResolver
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


class PassthroughFlowHandler(OAuthFlowHandler):
    """Forward the MCP client's ``Authorization`` header to the upstream API.

    No MCP-side OAuth server, no token store, no upstream credential exchange.

    Safety constraint: only correct when the gateway and the upstream share the same OAuth audience,
    i.e. the upstream validates the same access tokens the gateway accepts from MCP clients.
    The canonical safe case is the FastAPI integration where the gateway is mounted onto the app it exposes.

    Forwarding to a third-party upstream with a different audience is the "confused deputy" pattern.
    The MCP 2025-11-25 authorization spec explicitly forbids it per RFC 8707.
    For that case use ``authorization_code`` or ``client_credentials`` so the gateway mints its own upstream tokens.

    Selected only when ``auth.flow='passthrough'`` is set explicitly; the resolver never auto-selects it.
    """

    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        logger.debug(
            'Passthrough flow set up for "%s": MCP client Authorization forwarded verbatim',
            flow_context.entry.name,
        )
        return OAuthFlowSetup(resolver=PassthroughAuthResolver())
