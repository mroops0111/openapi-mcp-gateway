import collections.abc
import logging

from mcp.server.auth.middleware.auth_context import get_access_token

from ...scopes import ScopeRegistry, TokenGrant, UpstreamGrantSource, current_grant
from ...settings import AuthConfig
from ..assembly import (
    McpSetting,
    McpSettingsReader,
    OAuthFlowContext,
    OAuthFlowHandler,
    ServerAuthSetup,
    UpstreamPrincipal,
    UpstreamSetting,
)
from ..authorities import McpExternalIssuerAuthority
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from ..upstream.token_exchange import UpstreamExchangedToken, UpstreamTokenExchangeClient
from ..upstream.token_source import UpstreamTokenSource


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
        grant = current_grant()
        exchanged = await self._exchange_for_caller()
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

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'user'

    @property
    def upstream_settings(self) -> frozenset[UpstreamSetting]:
        return frozenset(
            {
                'client_id',
                'client_secret',
                'scopes',
                'required_scopes',
                'resource',
                'audience',
                'enforce_scopes',
                'scope_implies',
            }
        )

    @property
    def reads_spec_oauth_flow(self) -> bool:
        # No spec can declare token_exchange, and it talks to no upstream authorization server.
        # Consulting the spec would only report a misleading missing flow.
        return False

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return frozenset({'issuer', 'required_scopes', 'operation_scopes'})

    @property
    def mcp_settings_requirement(self) -> str:
        return 'auth.upstream.flow token_exchange, where another authorization server issues MCP tokens'

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        auth.mcp.refuse_scope_settings_for_issuer_tokens(server_name)

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

        authority = McpExternalIssuerAuthority()
        metadata = authority.discover(flow_context.auth_context)
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
            flow_context.scope_registry,
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
        return authority.attach_discovered(upstream_setup, flow_context.auth_context, metadata)
