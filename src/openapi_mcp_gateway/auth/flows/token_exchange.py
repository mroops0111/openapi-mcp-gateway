import collections.abc
import logging

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from ...scopes import ScopeRegistry, TokenGrant, UpstreamGrantSource, UpstreamGrantUnavailableError, read_current_grant
from ...settings import AuthConfig
from ..assembly import (
    AuthTypeContext,
    McpSetting,
    McpSettingsReader,
    ServerAuthSetup,
    UpstreamPrincipal,
    UpstreamSetting,
)
from ..external_issuer_setup import McpExternalIssuerSetup
from ..upstream.oauth_flow import UpstreamOAuthFlow
from ..upstream.resolver import UPSTREAM_CREDENTIAL_UNAVAILABLE, UpstreamTokenSourceAuthResolver
from ..upstream.token_exchange import (
    UpstreamExchangedToken,
    UpstreamTokenExchangeClient,
    UpstreamTokenExchangeError,
    UpstreamTokenExchangeRefusedError,
)
from ..upstream.token_source import UpstreamTokenSource
from .handler import OAuthFlowHandler


logger = logging.getLogger(__name__)


class TokenExchangeBridge(UpstreamTokenSource, UpstreamGrantSource):
    """Joins the caller's validated MCP token to the upstream token exchanged for it, for ``token_exchange``.

    Reads the token the MCP transport already verified for this request,
    so the exchange only ever runs on a credential the gateway has accepted for itself.
    Each exchange asks for the upstream scopes the operations the caller's MCP scopes open need,
    picked by ``scope_registry`` within ``ceiling`` as an ``authorization_code`` sign-in picks them.
    The scopes the exchange granted are the upstream grant a call is checked against.
    Returns no token when there is no authenticated caller,
    which lets the upstream reject the call rather than the gateway serving it anonymously.
    """

    def __init__(
        self,
        upstream: UpstreamTokenExchangeClient,
        scope_registry: ScopeRegistry,
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
    ) -> None:
        self.upstream = upstream
        self._scope_registry = scope_registry
        self._ceiling = list(ceiling)
        self._required_scopes = list(required_scopes)

    # UpstreamTokenSource

    async def get_token(self) -> str | None:
        exchanged = await self._exchange_for_caller()
        return exchanged.access_token if exchanged else None

    async def aclose(self) -> None:
        await self.upstream.aclose()

    # UpstreamGrantSource

    @property
    def records_user_grant(self) -> bool:
        return True

    async def read_call_grant(self) -> TokenGrant:
        grant = read_current_grant()
        try:
            exchanged = await self._exchange_for_caller()
        except UpstreamTokenExchangeRefusedError as error:
            # An issuer that will not grant every scope asked for may refuse the whole exchange rather than narrow it,
            # as Keycloak does, so the refusal is the call's answer rather than a server error.
            raise UpstreamGrantUnavailableError(str(error)) from error
        except (UpstreamTokenExchangeError, httpx.HTTPError) as error:
            # Any other failure is the gateway's, so its detail goes to the log and the caller learns only that much.
            logger.warning('Token exchange for a call failed: %s', error)
            raise UpstreamGrantUnavailableError(UPSTREAM_CREDENTIAL_UNAVAILABLE) from error
        if exchanged is None:
            return grant
        return grant._replace(upstream_scopes=frozenset(exchanged.scopes))

    async def _exchange_for_caller(self) -> UpstreamExchangedToken | None:
        access_token = get_access_token()
        if access_token is None:
            return None
        scopes = self._scope_registry.requested_upstream_scopes(
            self._ceiling, self._required_scopes, access_token.scopes
        )
        return await self.upstream.exchange(access_token.token, scopes)


class TokenExchangeFlowHandler(OAuthFlowHandler, McpSettingsReader):
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

    # No spec can declare token_exchange, and it talks to no upstream authorization server.
    # Consulting the spec would only report a misleading missing flow.
    reads_spec_oauth_flow = False
    # Each exchange states what the issuer granted for the upstream.
    records_user_grant = True

    def __init__(self) -> None:
        self._issuer_setup = McpExternalIssuerSetup()

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'user'

    @property
    def flow_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset(
            {
                'client_id',
                'client_secret',
                'scopes',
                'required_scopes',
                'resource',
                'audience',
            }
        )

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return self._issuer_setup.mcp_settings

    @property
    def mcp_settings_requirement(self) -> str:
        return 'auth.upstream.flow token_exchange, where another authorization server issues MCP tokens'

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        self._issuer_setup.refuse_unusable_settings(auth, server_name)

    def build(self, context: AuthTypeContext, upstream_oauth_flow: UpstreamOAuthFlow | None) -> ServerAuthSetup:
        entry = context.entry

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

        metadata = self._issuer_setup.discover(issuer)
        if not metadata.token_endpoint:
            raise ValueError(
                f'Server "{entry.name}": issuer "{issuer}" publishes no token_endpoint, '
                "so the gateway cannot exchange the caller's token for an upstream one."
            )

        bridge = TokenExchangeBridge(
            UpstreamTokenExchangeClient(
                token_endpoint=metadata.token_endpoint,
                client_id=client_id,
                client_secret=client_secret,
                audience_params=audience_params,
            ),
            context.scope_registry,
            entry.auth.upstream.scopes,
            entry.auth.upstream.required_scopes,
        )
        logger.debug(
            'Token exchange flow set up for "%s": issuer=%s exchange_target=%s upstream_scopes=%s',
            entry.name,
            metadata.issuer,
            audience_params,
            entry.auth.upstream.scopes,
        )
        upstream_setup = ServerAuthSetup(
            resolver=UpstreamTokenSourceAuthResolver(bridge), upstream_grant_source=bridge, on_shutdown=bridge.aclose
        )
        # The MCP half is the external issuer's, as on a server whose upstream sees only the gateway.
        return self._issuer_setup.attach(upstream_setup, context, metadata)
