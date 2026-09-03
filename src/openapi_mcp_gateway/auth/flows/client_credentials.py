import logging

from ..resolver import TokenSourceAuthResolver
from ..token_source import ClientCredentialsTokenSource
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


class ClientCredentialsFlowHandler(OAuthFlowHandler):
    """Build the service-level ``client_credentials`` setup.

    No MCP-side OAuth server is needed:
    the gateway uses its own credentials to fetch a single upstream access token shared across all MCP clients.
    The resulting setup carries only an ``AuthResolver`` plus an ``on_shutdown`` hook.
    The hook closes the underlying token source's HTTP client.
    """

    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        entry = flow_context.entry
        oauth_flow = flow_context.oauth_flow

        client_id = entry.auth.upstream.resolve_client_id()
        client_secret = entry.auth.upstream.resolve_client_secret()
        if not client_id or not client_secret:
            raise ValueError(
                f'Server "{entry.name}": client_credentials flow requires client_id and client_secret. '
                'Set them directly or use ${ENV_VAR} syntax.'
            )
        if not oauth_flow.token_url:
            raise ValueError(
                f'Server "{entry.name}": client_credentials flow requires token_url. '
                'Provide auth.upstream.token_url or add it to the spec securitySchemes.'
            )

        scopes = entry.auth.upstream.scopes or list(oauth_flow.scopes.keys())
        audience_params = entry.auth.upstream.resolve_audience_params()
        token_source = ClientCredentialsTokenSource(
            token_url=oauth_flow.token_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            audience_params=audience_params,
        )

        logger.debug(
            'Client credentials flow set up for "%s": token=%s scopes=%s audience_params=%s',
            entry.name,
            oauth_flow.token_url,
            scopes,
            audience_params,
        )

        return OAuthFlowSetup(
            resolver=TokenSourceAuthResolver(token_source),
            on_shutdown=token_source.aclose,
        )
