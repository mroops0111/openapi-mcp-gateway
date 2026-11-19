import logging

import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata

from ..assembly import OAuthFlowContext, OAuthFlowHandler, ServerAuthSetup
from ..mcp.external_issuer import McpExternalIssuerTokenVerifier, fetch_mcp_external_issuer_metadata
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from ..upstream.token_exchange import UpstreamTokenExchangeClient
from ..upstream.token_source import UpstreamTokenSource


logger = logging.getLogger(__name__)


class TokenExchangeBridge(UpstreamTokenSource):
    """Joins the caller's validated MCP token to the upstream token exchanged for it, for ``token_exchange``.

    Reads the token the MCP transport already verified for this request,
    so the exchange only ever runs on a credential the gateway has accepted for itself.
    Returns no token when there is no authenticated caller,
    which lets the upstream reject the call rather than the gateway serving it anonymously.
    """

    def __init__(self, upstream: UpstreamTokenExchangeClient) -> None:
        self.upstream = upstream

    async def get_token(self) -> str | None:
        access_token = get_access_token()
        if access_token is None:
            return None
        return await self.upstream.exchange(access_token.token)

    async def aclose(self) -> None:
        await self.upstream.aclose()


class TokenExchangeFlowHandler(OAuthFlowHandler):
    """Delegate this MCP endpoint's authorization to an issuer the gateway does not own.

    The gateway issues no credentials here.
    It validates tokens an external authorization server minted for its own canonical URI,
    then exchanges each one under RFC 8693 for a second token naming the upstream API.

    Both legs stay separate, as the MCP spec requires.
    The caller's token is accepted only because it names this endpoint,
    and it is never the token that reaches the upstream.
    Unlike ``authorization_code`` the gateway is not a second credential issuer,
    so revoking a user at the issuer takes effect without waiting for a gateway-minted token to expire.
    """

    upstream_principal = 'user'
    mcp_settings = frozenset({'issuer', 'required_scopes'})
    mcp_settings_requirement = 'auth.upstream.flow token_exchange, where another authorization server issues MCP tokens'

    def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
        entry = flow_context.entry

        issuer = entry.auth.mcp.resolve_issuer()
        if not issuer:
            raise ValueError(
                f'Server "{entry.name}": token_exchange flow requires auth.mcp.issuer, '
                'the authorization server that mints tokens for this MCP endpoint.'
            )

        audience_params = entry.auth.upstream.resolve_audience_params()
        if not audience_params:
            raise ValueError(
                f'Server "{entry.name}": token_exchange flow requires auth.upstream.resource or auth.upstream.audience, '
                'naming the upstream API that exchanged tokens are for. '
                'Without it the authorization server mints a token for its own default audience, '
                'which the upstream refuses.'
            )

        client_id = entry.auth.upstream.resolve_client_id()
        client_secret = entry.auth.upstream.resolve_client_secret()
        if not client_id or not client_secret:
            raise ValueError(
                f'Server "{entry.name}": token_exchange flow requires client_id and client_secret. '
                "The gateway authenticates as itself to exchange the caller's token, "
                'and authorization servers require a confidential client for that grant.'
            )

        metadata = fetch_mcp_external_issuer_metadata(issuer)
        if not metadata.token_endpoint:
            raise ValueError(
                f'Server "{entry.name}": issuer "{issuer}" publishes no token_endpoint, '
                "so the gateway cannot exchange the caller's token for an upstream one."
            )

        # RFC 8707 §2: the canonical URI of this MCP server.
        # A client asks the issuer for this, so it is what the issuer stamps into the token's audience.
        gateway_url = flow_context.gateway_url.rstrip('/')
        canonical_uri = f'{gateway_url}{flow_context.mount_path}/mcp'

        # The two scope lists point in opposite directions and must not be conflated.
        # Demanding the upstream scopes of the caller would lock out every client that did not match,
        # since the deployment picks what to request from the issuer but not what a caller registered with.
        mcp_endpoint_scopes = list(entry.auth.mcp.required_scopes)
        verifier = McpExternalIssuerTokenVerifier(
            issuer=metadata.issuer,
            audience=canonical_uri,
            jwks_uri=metadata.jwks_uri,
            required_scopes=mcp_endpoint_scopes,
        )
        bridge = TokenExchangeBridge(
            UpstreamTokenExchangeClient(
                token_endpoint=metadata.token_endpoint,
                client_id=client_id,
                client_secret=client_secret,
                audience_params=audience_params,
                scopes=entry.auth.upstream.scopes,
            )
        )

        settings = AuthSettings(
            issuer_url=pydantic.AnyHttpUrl(metadata.issuer),
            resource_server_url=pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}'),
            required_scopes=mcp_endpoint_scopes or None,
        )

        logger.debug(
            'Token exchange flow set up for "%s": issuer=%s resource=%s exchange_target=%s '
            'required_scopes=%s upstream_scopes=%s',
            entry.name,
            metadata.issuer,
            canonical_uri,
            audience_params,
            mcp_endpoint_scopes,
            entry.auth.upstream.scopes,
        )

        return ServerAuthSetup(
            resolver=UpstreamTokenSourceAuthResolver(bridge),
            settings=settings,
            verifier=verifier,
            # Validated from strings so a path-less issuer keeps its exact form.
            # RFC 8414 §2 compares issuers by exact string, and ``AnyHttpUrl`` would append a slash.
            protected_resource=ProtectedResourceMetadata.model_validate(
                {
                    'resource': canonical_uri,
                    'authorization_servers': [metadata.issuer],
                    # RFC 9728 defines this as what a client must obtain for this endpoint,
                    # so it is the inbound list, and a registering client reads it to know what to ask for.
                    'scopes_supported': mcp_endpoint_scopes or None,
                }
            ),
            mcp_summary=f'issuer ({metadata.issuer})',
            on_shutdown=bridge.aclose,
        )
