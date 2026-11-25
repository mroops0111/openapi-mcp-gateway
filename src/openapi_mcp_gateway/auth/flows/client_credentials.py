import logging

from ...settings import UpstreamAuthConfig
from ..assembly import AuthTypeContext, ServerAuthSetup, UpstreamPrincipal, UpstreamSetting
from ..upstream.client_credentials import UpstreamClientCredentialsTokenSource
from ..upstream.oauth_flow import UpstreamOAuthFlow
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from .handler import OAuthFlowHandler, require_upstream_oauth_flow


logger = logging.getLogger(__name__)


class UpstreamClientCredentialsFlowHandler(OAuthFlowHandler):
    """Build the service-level ``client_credentials`` setup.

    No MCP-side OAuth server is needed.
    The gateway uses its own credentials to fetch a single upstream access token, shared across all MCP clients.
    The resulting setup carries only an ``UpstreamAuthResolver`` plus an ``on_shutdown`` hook.
    The hook closes the underlying token source's HTTP client.
    """

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'gateway'

    @classmethod
    def synthesises_from(cls, config: UpstreamAuthConfig) -> bool:
        # A token URL is all it needs, so it is the flow a config naming no authorization URL describes.
        return True

    @property
    def flow_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset({'client_id', 'client_secret', 'token_url', 'scopes', 'resource', 'audience'})

    def build(self, context: AuthTypeContext, upstream_oauth_flow: UpstreamOAuthFlow | None) -> ServerAuthSetup:
        entry = context.entry
        oauth_flow = require_upstream_oauth_flow(context, upstream_oauth_flow)

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

        scopes = entry.auth.upstream.scopes or list(oauth_flow.scope_descriptions)
        audience_params = entry.auth.upstream.resolve_audience_params()
        token_source = UpstreamClientCredentialsTokenSource(
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

        return ServerAuthSetup(
            resolver=UpstreamTokenSourceAuthResolver(token_source),
            on_shutdown=token_source.aclose,
        )
