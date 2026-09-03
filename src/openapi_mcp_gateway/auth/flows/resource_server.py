import logging

import pydantic
from mcp.server.auth.settings import AuthSettings

from ..oidc import JWKSTokenVerifier, fetch_issuer_metadata
from ..resolver import TokenExchangeAuthResolver
from ..token_source import TokenExchangeTokenSource
from .base import AdvertisedResource, OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


class ResourceServerFlowHandler(OAuthFlowHandler):
    """Delegate this MCP endpoint's authorization to an issuer the gateway does not own.

    The gateway issues no credentials here.
    It validates tokens an external authorization server minted for its own canonical URI,
    then exchanges each one under RFC 8693 for a second token naming the upstream API.

    Both legs stay separate, which is what the MCP spec requires:
    the caller's token is accepted only because it names this endpoint,
    and it is never the token that reaches the upstream.
    Unlike ``authorization_code`` the gateway is not a second credential issuer,
    so revoking a user at the issuer takes effect without waiting for a gateway-minted token to expire.
    """

    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        entry = flow_context.entry

        issuer = entry.auth.resolve_issuer()
        if not issuer:
            raise ValueError(
                f'Server "{entry.name}": resource_server flow requires auth.issuer, '
                'the authorization server that mints tokens for this MCP endpoint.'
            )

        audience_params = entry.auth.resolve_upstream_audience_params()
        if not audience_params:
            raise ValueError(
                f'Server "{entry.name}": resource_server flow requires auth.resource or auth.audience, '
                'naming the upstream API that exchanged tokens are for. '
                'Without it the authorization server mints a token for its own default audience, '
                'which the upstream refuses.'
            )

        client_id = entry.auth.resolve_client_id()
        client_secret = entry.auth.resolve_client_secret()
        if not client_id or not client_secret:
            raise ValueError(
                f'Server "{entry.name}": resource_server flow requires client_id and client_secret. '
                'The gateway authenticates as itself to exchange the caller\'s token, '
                'and authorization servers require a confidential client for that grant.'
            )

        metadata = fetch_issuer_metadata(issuer)
        if not metadata.token_endpoint:
            raise ValueError(
                f'Server "{entry.name}": issuer "{issuer}" publishes no token_endpoint, '
                'so the gateway cannot exchange the caller\'s token for an upstream one.'
            )

        # RFC 8707 §2: the canonical URI of this MCP server, which is what a client asks the issuer for
        # and therefore what the issuer stamps into the token's audience.
        gateway_url = flow_context.gateway_url.rstrip('/')
        canonical_uri = f'{gateway_url}{flow_context.mount_path}/mcp'

        verifier = JWKSTokenVerifier(
            issuer=metadata.issuer,
            audience=canonical_uri,
            jwks_uri=metadata.jwks_uri,
            required_scopes=entry.auth.scopes,
        )
        token_source = TokenExchangeTokenSource(
            token_endpoint=metadata.token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            audience_params=audience_params,
            scopes=entry.auth.scopes,
        )

        settings = AuthSettings(
            issuer_url=pydantic.AnyHttpUrl(metadata.issuer),
            resource_server_url=pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}'),
            required_scopes=list(entry.auth.scopes) or None,
        )

        logger.debug(
            'Resource server flow set up for "%s": issuer=%s resource=%s exchange_target=%s',
            entry.name,
            metadata.issuer,
            canonical_uri,
            audience_params,
        )

        return OAuthFlowSetup(
            resolver=TokenExchangeAuthResolver(token_source),
            settings=settings,
            verifier=verifier,
            advertised_resource=AdvertisedResource(
                resource=canonical_uri,
                authorization_servers=(metadata.issuer,),
                scopes_supported=tuple(entry.auth.scopes),
            ),
            on_shutdown=token_source.aclose,
        )
