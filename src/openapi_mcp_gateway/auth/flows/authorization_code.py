import logging
import secrets
import time

import pydantic
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenError, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, RevocationOptions
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ..mcp.authorization_code import (
    McpAuthorizationCodeContinuation,
    McpAuthorizationCodeRoutes,
    McpAuthorizationCodeServer,
    McpNextSignIn,
)
from ..mcp.client import McpClientDirectory, McpDynamicClientRegistry, McpPreRegisteredClientDirectory
from ..mcp.consent import PENDING_AUTHORIZATION_TTL, BrowserCookie, PendingAuthorization
from ..mcp.consent_page import redirect_without_cache, render_error_page
from ..mcp.record_origin import McpRecordOrigin
from ..mcp.token_issuer import MCP_DEFAULT_SCOPE, McpTokenAttachment, McpTokenIssuer, McpTokenPolicy
from ..setup import AuthRoutes, ServerAuthSetup
from ..upstream.authorization_code import UpstreamAuthorizationCodeClient
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from ..upstream.token_source import UpstreamTokenSource
from .base import OAuthFlowContext, OAuthFlowHandler


logger = logging.getLogger(__name__)


# Where the upstream sends the browser back, under the server's own path.
CALLBACK_PATH = '/auth/callback'

# Where the upstream credentials behind each MCP token are found, as links ``McpTokenIssuer`` carries.
UPSTREAM_ACCESS_TOKEN = 'upstream_access_token'  # noqa: S105
UPSTREAM_REFRESH_TOKEN = 'upstream_refresh_token'  # noqa: S105

# The upstream authorization errors that mean the same to the MCP client, and so reach it unchanged.
_UPSTREAM_FORWARDED_ERRORS = frozenset({'access_denied', 'temporarily_unavailable'})

# Why the upstream's redirect cannot be finished, as the user reads it.
_INCOMPLETE_CALLBACK = 'The sign-in service sent you back without an answer.'


class AuthorizationCodeBridge(McpAuthorizationCodeContinuation, UpstreamTokenSource):
    """Joins the gateway's MCP sign-in to the user's upstream sign-in, for ``authorization_code``.

    Once the gateway's consent page is approved, the browser goes on to the upstream.
    When it comes back, the upstream token is redeemed and the MCP code is minted carrying it,
    so each MCP access token stands for one user's upstream token.
    The upstream token is a separate credential from the ``mcp_...`` token handed to the MCP client,
    which is what the MCP authorization spec requires of a server calling an upstream API.

    ``tokens`` is the MCP side's issuer rather than its server, so neither holds the other.
    """

    def __init__(self, tokens: McpTokenIssuer, upstream: UpstreamAuthorizationCodeClient) -> None:
        self.tokens = tokens
        self.upstream = upstream

    # McpAuthorizationCodeContinuation

    def describe_next_sign_in(self) -> McpNextSignIn:
        return McpNextSignIn(
            host=self.upstream.host,
            scopes=list(self.upstream.scopes),
            scope_descriptions=self.upstream.scope_descriptions,
        )

    async def continue_after_consent(self, pending: PendingAuthorization) -> str:
        """Send the browser to the upstream, under a fresh ``state`` only the browser behind ``pending`` can finish.

        The client's own ``state`` is never sent upstream.
        It is kept with the sign-in and handed back to the client at the end, as RFC 6749 requires.
        """
        state = secrets.token_urlsafe(32)
        await self.tokens.store.set('upstream_auth_state', state, pending.model_dump(), ttl=PENDING_AUTHORIZATION_TTL)
        return self.upstream.build_authorize_url(state)

    async def refresh_attachment(self, client_id: str, attachment: McpTokenAttachment) -> McpTokenAttachment:
        """Refresh the upstream token too when it has expired, keeping the upstream refresh token if none is new."""
        upstream_access_token = attachment.links[UPSTREAM_ACCESS_TOKEN]
        if await self.tokens.store.get(UPSTREAM_ACCESS_TOKEN, upstream_access_token):
            return attachment
        upstream_refresh_token = attachment.links.get(UPSTREAM_REFRESH_TOKEN)
        if not upstream_refresh_token:
            logger.warning('OAuth refresh rejected: reason=upstream_expired_no_refresh client_id=%s', client_id)
            raise TokenError(
                error='invalid_grant',
                error_description='Upstream token expired and no refresh token available; re-authenticate',
            )
        refreshed = await self.upstream.refresh(upstream_refresh_token)
        await self._store_upstream_access_token(client_id, refreshed.access_token, refreshed.expires_in)
        return McpTokenAttachment(
            upstream_scopes=refreshed.resolve_granted_scopes(attachment.upstream_scopes),
            links=_build_upstream_links(refreshed.access_token, refreshed.refresh_token or upstream_refresh_token),
        )

    # UpstreamTokenSource

    async def get_token(self) -> str | None:
        """The upstream token behind the MCP access token of the call being handled."""
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return None
        return await self.tokens.load_access_token_link(mcp_access_token.token, UPSTREAM_ACCESS_TOKEN)

    # The upstream's redirect back

    async def handle_upstream_callback(
        self, code: str, state: str, browser_id: str | None, iss: str | None = None
    ) -> str:
        """Finish the browser redirect by swapping the upstream ``code`` for an MCP authorization code.

        Validates ``state`` against the browser that approved the sign-in and the RFC 9207 ``iss``,
        redeems the code at the upstream token endpoint,
        and returns the client redirect carrying an MCP code that links the upstream tokens.
        The checks run before the code leaves the gateway,
        since a mix-up attack succeeds the moment the code reaches the wrong token endpoint.
        """
        pending = await self._pending_upstream(state, browser_id)
        self.upstream.validate_issuer(iss)

        upstream_token = await self.upstream.exchange_code(code)
        await self._store_upstream_access_token(
            pending.client_id, upstream_token.access_token, upstream_token.expires_in
        )
        redirect_uri = await self.tokens.complete_authorization(
            pending,
            McpTokenAttachment(
                upstream_scopes=upstream_token.resolve_granted_scopes(self.upstream.scopes),
                links=_build_upstream_links(upstream_token.access_token, upstream_token.refresh_token),
            ),
        )
        await self.tokens.store.delete('upstream_auth_state', state)

        logger.info(
            'Upstream OAuth callback handled: client_id=%s expires_in=%s refresh=%s',
            pending.client_id,
            upstream_token.expires_in,
            bool(upstream_token.refresh_token),
        )
        return redirect_uri

    async def handle_upstream_error(
        self, error: str, state: str, browser_id: str | None, error_description: str | None = None
    ) -> str:
        """End a sign-in the upstream refused, and return the redirect telling the client, per RFC 6749 §4.1.2.1.

        ``access_denied`` and ``temporarily_unavailable`` mean the same to the client and pass through.
        Any other error concerns the gateway's own request to the upstream, so the client hears ``server_error``.
        """
        pending = await self._pending_upstream(state, browser_id)
        await self.tokens.store.delete('upstream_auth_state', state)
        logger.info('Upstream OAuth refused the sign-in: client_id=%s error=%s', pending.client_id, error)
        forwarded = error if error in _UPSTREAM_FORWARDED_ERRORS else 'server_error'
        return construct_redirect_uri(
            pending.redirect_uri,
            error=forwarded,
            error_description=error_description or f'The upstream authorization server answered {error}',
            state=pending.client_state,
            iss=self.tokens.issuer,
        )

    async def _pending_upstream(self, state: str, browser_id: str | None) -> PendingAuthorization:
        """The sign-in the upstream's callback under ``state`` finishes, which ``browser_id`` must have approved.

        Raises ``HTTPException`` when ``state`` names none of this server's sign-ins against the current upstream,
        including one recorded before sign-ins were tied to a browser, or when another browser delivers it.
        A sign-in recorded against another upstream is refused outright,
        since its code would otherwise be redeemed at a token endpoint that never issued it.
        """
        pending = await self.tokens.load_pending('upstream_auth_state', state)
        if pending is None:
            logger.warning('OAuth callback rejected: reason=invalid_state')
            raise HTTPException(400, 'This sign-in has expired or was already finished')
        if browser_id is None or pending.browser_id != browser_id:
            logger.warning('OAuth callback rejected: reason=other_browser client_id=%s', pending.client_id)
            raise HTTPException(400, 'This sign-in was started in another browser')
        return pending

    async def _store_upstream_access_token(self, client_id: str, token: str, expires_in: int) -> None:
        """Record that the upstream ``token`` is live, for as long as the upstream says it is."""
        await self.tokens.store.set(
            UPSTREAM_ACCESS_TOKEN,
            token,
            {'token': token, 'client_id': client_id, 'expires_at': int(time.time()) + expires_in},
            ttl=expires_in,
        )


class AuthorizationCodeCallbackRoutes(AuthRoutes):
    """The page the upstream sends the browser back to, which finishes the sign-in ``AuthorizationCodeBridge`` began.

    The browser cookie ties the callback to the browser that approved the consent page.
    """

    def __init__(self, bridge: AuthorizationCodeBridge, cookie: BrowserCookie) -> None:
        self._bridge = bridge
        self._cookie = cookie

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(CALLBACK_PATH, methods=['GET'])(self.finish_upstream)

    async def finish_upstream(self, request: Request) -> Response:
        """Take the upstream's redirect, and send the browser on to the MCP client with its code or the refusal.

        A failure the client cannot be told about is shown on the gateway's error page.
        """
        query = request.query_params
        state = query.get('state')
        browser_id = self._cookie.read(request)
        try:
            if state and (error := query.get('error')):
                redirect_uri = await self._bridge.handle_upstream_error(
                    error, state, browser_id, query.get('error_description')
                )
            elif state and (code := query.get('code')):
                redirect_uri = await self._bridge.handle_upstream_callback(code, state, browser_id, query.get('iss'))
            else:
                return render_error_page(_INCOMPLETE_CALLBACK, 400)
        except HTTPException as exc:
            return render_error_page(exc.detail, exc.status_code)
        return redirect_without_cache(redirect_uri, 302)


class AuthorizationCodeFlowHandler(OAuthFlowHandler):
    """Assemble the per-user ``authorization_code`` flow, whose two directions are bound.

    It builds the gateway's MCP authorization server, the gateway's upstream OAuth client,
    and the ``AuthorizationCodeBridge`` that joins them.
    """

    def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
        entry = flow_context.entry
        oauth_flow = flow_context.require_upstream_oauth_flow()
        upstream_config = entry.auth.upstream
        mcp_config = entry.auth.mcp

        client_id = upstream_config.resolve_client_id()
        client_secret = upstream_config.resolve_client_secret()
        if not client_id or not client_secret:
            raise ValueError(
                f'Server "{entry.name}": authorization_code flow requires client_id and client_secret. '
                'Set them directly or use ${ENV_VAR} syntax.'
            )

        if not oauth_flow.authorization_url:
            raise ValueError(
                f'Server "{entry.name}": authorization_code flow requires authorization_url. '
                'Provide auth.upstream.authorization_url or add it to the spec securitySchemes.'
            )
        if not oauth_flow.token_url:
            raise ValueError(f'Server "{entry.name}": authorization_code flow requires token_url.')

        gateway_url = flow_context.gateway_url.rstrip('/')
        upstream = UpstreamAuthorizationCodeClient(
            authorization_url=oauth_flow.authorization_url,
            token_url=oauth_flow.token_url,
            client_id=client_id,
            client_secret=client_secret,
            callback_url=f'{gateway_url}{flow_context.mount_path}{CALLBACK_PATH}',
            scopes=list(upstream_config.scopes),
            scope_descriptions=oauth_flow.scope_descriptions,
            audience_params=upstream_config.resolve_audience_params(),
            issuer=upstream_config.resolve_issuer(),
        )

        # The scopes every token carries and every request must hold, granted on top of a client's own scopes.
        # With auth.mcp.operation_scopes there are none, since each operation names its own scopes instead.
        mcp_endpoint_scopes = (
            [] if mcp_config.operation_scopes is not None else list(mcp_config.required_scopes) or [MCP_DEFAULT_SCOPE]
        )
        server_url = pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}')
        # The same string the metadata publishes as ``issuer``,
        # since RFC 9207 compares the two without any normalisation.
        issuer = str(server_url)
        tokens = McpTokenIssuer(
            store=flow_context.store,
            origin=McpRecordOrigin(issuer=issuer, upstream_authorization_server=upstream.authorization_server),
            policy=McpTokenPolicy(
                scopes=mcp_endpoint_scopes,
                access_token_ttl=mcp_config.access_token_ttl,
                refresh_token_ttl=mcp_config.refresh_token_ttl,
            ),
        )
        bridge = AuthorizationCodeBridge(tokens, upstream)
        clients = _build_client_directory(flow_context, mcp_endpoint_scopes, issuer)
        server = McpAuthorizationCodeServer(tokens, clients, bridge, server_name=entry.name)
        # One cookie for both pages, since the callback must read what the consent page set.
        cookie = BrowserCookie(secure=issuer.startswith('https://'))
        settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=clients.build_registration_options(mcp_endpoint_scopes),
            required_scopes=mcp_endpoint_scopes,
        )

        logger.debug(
            'Authorization code flow set up for "%s": authorize=%s token=%s scopes=%s audience_params=%s',
            entry.name,
            oauth_flow.authorization_url,
            oauth_flow.token_url,
            upstream_config.scopes,
            upstream.audience_params,
        )

        return ServerAuthSetup(
            resolver=UpstreamTokenSourceAuthResolver(bridge),
            provider=server,
            settings=settings,
            routes=[
                McpAuthorizationCodeRoutes(server, flow_context.mcp_scope_policy, cookie),
                AuthorizationCodeCallbackRoutes(bridge, cookie),
            ],
        )


def _build_client_directory(
    flow_context: OAuthFlowContext, mcp_endpoint_scopes: list[str], issuer: str
) -> McpClientDirectory:
    """The clients ``auth.mcp.clients`` declares, or Dynamic Client Registration when it declares none."""
    entry = flow_context.entry
    if entry.auth.mcp.clients:
        return McpPreRegisteredClientDirectory(entry.auth.mcp.clients, mcp_endpoint_scopes)
    logger.warning(
        'Server "%s" accepts Dynamic Client Registration, which the MCP 2026-07-28 spec deprecates. '
        'Declare the clients allowed to sign in under auth.mcp.clients instead.',
        entry.name,
    )
    return McpDynamicClientRegistry(flow_context.store, issuer)


def _build_upstream_links(access_token: str, refresh_token: str | None) -> dict[str, str]:
    """The links an MCP token carries to the upstream tokens behind it."""
    links = {UPSTREAM_ACCESS_TOKEN: access_token}
    if refresh_token:
        links[UPSTREAM_REFRESH_TOKEN] = refresh_token
    return links
