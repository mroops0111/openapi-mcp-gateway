import logging

import pydantic
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata

from ..oidc import JWKSTokenVerifier, fetch_issuer_metadata
from ..resolver import TokenExchangeAuthResolver
from ..token_source import TokenExchangeTokenSource
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


class TokenExchangeFlowHandler(OAuthFlowHandler):
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
                f'Server "{entry.name}": token_exchange flow requires auth.issuer, '
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

        metadata = fetch_issuer_metadata(issuer)
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
        required_scopes = list(entry.auth.required_scopes)
        verifier = JWKSTokenVerifier(
            issuer=metadata.issuer,
            audience=canonical_uri,
            jwks_uri=metadata.jwks_uri,
            required_scopes=required_scopes,
        )
        token_source = TokenExchangeTokenSource(
            token_endpoint=metadata.token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            audience_params=audience_params,
            scopes=entry.auth.upstream.scopes,
        )

        settings = AuthSettings(
            issuer_url=pydantic.AnyHttpUrl(metadata.issuer),
            resource_server_url=pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}'),
            required_scopes=required_scopes or None,
        )

        logger.debug(
            'Token exchange flow set up for "%s": issuer=%s resource=%s exchange_target=%s '
            'required_scopes=%s upstream_scopes=%s',
            entry.name,
            metadata.issuer,
            canonical_uri,
            audience_params,
            required_scopes,
            entry.auth.upstream.scopes,
        )

        return OAuthFlowSetup(
            resolver=TokenExchangeAuthResolver(token_source),
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
                    'scopes_supported': required_scopes or None,
                }
            ),
            on_shutdown=token_source.aclose,
        )
