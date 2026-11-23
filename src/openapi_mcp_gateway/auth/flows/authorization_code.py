import logging
import secrets
import time
import urllib.parse

import anyio
import pydantic
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenError, construct_redirect_uri
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthMetadata
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ...scopes import ScopeRegistry
from ...stores.base import TokenStore
from ..assembly import (
    AuthRoutes,
    McpSetting,
    McpSettingsReader,
    OAuthFlowContext,
    OAuthFlowHandler,
    ServerAuthSetup,
    UpstreamPrincipal,
)
from ..mcp.authorization_code import (
    McpAuthorizationCodeContinuation,
    McpAuthorizationCodeRoutes,
    McpAuthorizationCodeServer,
    McpNextSignIn,
)
from ..mcp.client import McpClientDirectory, McpClientDirectoryChain, McpDynamicClientRegistry
from ..mcp.consent import PENDING_AUTHORIZATION_TTL, BrowserCookie, PendingAuthorization
from ..mcp.consent_page import redirect_without_cache, render_error_page, render_signed_in_again_page
from ..mcp.record_origin import McpRecordOrigin
from ..mcp.token_issuer import McpTokenAttachment, McpTokenIssuer, McpTokenPolicy
from ..settings_mapping import (
    build_mcp_client_directory,
    build_mcp_client_metadata_directory,
    build_mcp_token_policy,
)
from ..upstream.authorization_code import UpstreamAuthorizationCodeClient, UpstreamTokenResponse
from ..upstream.reauthorization import UpstreamReauthorization, UpstreamReauthorizer
from ..upstream.rejection import UpstreamRejection
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from ..upstream.token_source import UpstreamTokenSource


logger = logging.getLogger(__name__)


# Where the upstream sends the browser back, under the server's own path.
CALLBACK_PATH = '/auth/callback'

# The names under which each MCP token links the upstream tokens behind it.
UPSTREAM_ACCESS_TOKEN_LINK = 'upstream_access_token'  # noqa: S105
UPSTREAM_REFRESH_TOKEN_LINK = 'upstream_refresh_token'  # noqa: S105

# Where a sign-in waits while the browser is at the upstream, keyed by the ``state`` sent there.
UPSTREAM_AUTH_STATE_NAMESPACE = 'upstream_auth_state'

# Where the user starts a new upstream sign-in for an MCP token the client already holds, under the server's own path.
REAUTHORIZE_PATH = '/auth/reauthorize'

# Where such a sign-in waits, keyed by its id, and the ``state`` it sends to the upstream.
UPSTREAM_REAUTHORIZATION_NAMESPACE = 'upstream_reauthorization'
UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE = 'upstream_reauthorization_state'


# The upstream authorization errors that mean the same to the MCP client, and so reach it unchanged.
_UPSTREAM_FORWARDED_ERRORS = frozenset({'access_denied', 'temporarily_unavailable'})

# Why the upstream's redirect cannot be finished, as the user reads it.
_INCOMPLETE_CALLBACK = 'The sign-in service sent you back without an answer.'

# How often a call waiting for a new upstream sign-in checks whether it finished, in seconds.
_REAUTHORIZATION_POLL_INTERVAL = 0.5


class PendingReauthorization(pydantic.BaseModel):
    """A new upstream sign-in for an MCP access token the client already holds.

    ``access_token`` is the token whose upstream credential the sign-in replaces.
    ``browser_id`` is the browser the token's first sign-in ran in, the only one that may finish this one,
    which is how the spec's phishing guidance is met without the gateway knowing who the user is.
    ``scopes`` are what the sign-in asks the upstream for.
    ``completed`` turns true once the new upstream token is in place.
    """

    reauthorization_id: str
    access_token: str
    browser_id: str
    scopes: list[str]
    origin: McpRecordOrigin
    completed: bool = False


class AuthorizationCodeBridge(McpAuthorizationCodeContinuation, UpstreamTokenSource, UpstreamReauthorizer):
    """Joins the gateway's MCP sign-in to the user's upstream sign-in, for ``authorization_code``.

    Once the gateway's consent page is approved, the browser goes on to the upstream.
    When it comes back, the upstream token is redeemed and the MCP code is minted carrying it,
    so each MCP access token stands for one user's upstream token.
    The upstream token is a separate credential from the ``mcp_...`` token handed to the MCP client,
    which is what the MCP authorization spec requires of a server calling an upstream API.

    ``tokens`` is the MCP side's issuer rather than its server, so neither holds the other.
    ``scope_registry`` chooses what each sign-in asks the upstream for, from the MCP scopes it will grant.
    ``store`` keeps the sign-ins waiting at the upstream, which belong to neither side alone.
    ``reauthorize_url`` is the gateway page where the user starts a new upstream sign-in for a token already held.
    """

    def __init__(
        self,
        tokens: McpTokenIssuer,
        upstream: UpstreamAuthorizationCodeClient,
        scope_registry: ScopeRegistry,
        store: TokenStore,
        reauthorize_url: str,
    ) -> None:
        self.tokens = tokens
        self.upstream = upstream
        self.scope_registry = scope_registry
        self._store = store
        self._reauthorize_url = reauthorize_url

    # McpAuthorizationCodeContinuation

    def describe_next_sign_in(self) -> McpNextSignIn:
        return McpNextSignIn(host=self.upstream.host, scope_descriptions=self.upstream.scope_descriptions)

    def select_next_sign_in_scopes(self, mcp_scopes: list[str]) -> list[str]:
        return self.scope_registry.upstream_sign_in_scopes(
            self.upstream.scopes, self.upstream.required_scopes, mcp_scopes
        )

    async def continue_after_consent(self, pending_authorization: PendingAuthorization) -> str:
        """Send the browser to the upstream under a fresh ``state``.

        Only the browser behind ``pending_authorization`` can finish the sign-in under it.
        The client's own ``state`` is never sent upstream.
        It is kept with the sign-in and handed back to the client at the end, as RFC 6749 requires.
        """
        state = secrets.token_urlsafe(32)
        await self._store.set(
            UPSTREAM_AUTH_STATE_NAMESPACE, state, pending_authorization.model_dump(), ttl=PENDING_AUTHORIZATION_TTL
        )
        return self.upstream.build_authorize_url(state, pending_authorization.next_sign_in_scopes)

    async def refresh_attachment(self, client_id: str, attachment: McpTokenAttachment) -> McpTokenAttachment:
        """Renew the upstream token too once less than half an MCP access token's lifetime remains of it.

        Renewing that early means a client that refreshes ahead of expiry gets a token worth using,
        rather than one capped at the few minutes left, which it would refresh again at once.
        Until the upstream token is gone, a renewal that cannot happen keeps the current one,
        and once it is gone the client hears ``invalid_grant`` and signs in again.
        The upstream refresh token is kept when the upstream sends no new one.
        """
        expires_at = attachment.links_expire_at
        if expires_at is None:
            return attachment
        remaining = expires_at - time.time()
        if remaining >= self.tokens.policy.access_token_ttl / 2:
            return attachment
        upstream_refresh_token = attachment.links.get(UPSTREAM_REFRESH_TOKEN_LINK)
        try:
            if not upstream_refresh_token:
                raise HTTPException(400, 'The upstream issued no refresh token')
            refreshed = await self.upstream.refresh(upstream_refresh_token)
        except HTTPException as exc:
            if remaining > 0:
                return attachment
            logger.warning(
                'OAuth refresh rejected: reason=upstream_expired client_id=%s detail=%s', client_id, exc.detail
            )
            raise TokenError(
                error='invalid_grant',
                error_description='The upstream sign-in expired and could not be renewed; sign in again',
            ) from exc
        return _renewed_attachment(attachment, refreshed, upstream_refresh_token)

    # UpstreamTokenSource

    async def get_token(self) -> str | None:
        """The upstream token behind the MCP access token of the call being handled."""
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return None
        return await self.tokens.load_access_token_link(mcp_access_token.token, UPSTREAM_ACCESS_TOKEN_LINK)

    # UpstreamReauthorizer

    async def renew(self) -> bool:
        """Refresh the upstream token behind the call's MCP token with the refresh token the upstream issued."""
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return False
        attachment = await self.tokens.load_access_token_attachment(mcp_access_token.token)
        upstream_refresh_token = attachment.links.get(UPSTREAM_REFRESH_TOKEN_LINK) if attachment else None
        if attachment is None or not upstream_refresh_token:
            return False
        try:
            refreshed = await self.upstream.refresh(upstream_refresh_token)
        except HTTPException as exc:
            logger.info('Upstream token renewal refused: detail=%s', exc.detail)
            return False
        renewed = _renewed_attachment(attachment, refreshed, upstream_refresh_token)
        return await self.tokens.replace_attachment(mcp_access_token.token, renewed)

    async def start(self, rejection: UpstreamRejection) -> UpstreamReauthorization | None:
        """Record a new upstream sign-in for the call's MCP token, and return the page that starts it.

        It asks for what a first sign-in with the token's MCP scopes would, what the upstream granted so far,
        and what the refusal names, so a new sign-in never narrows the grant.
        """
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return None
        attachment = await self.tokens.load_access_token_attachment(mcp_access_token.token)
        if attachment is None or attachment.browser_id is None:
            return None
        if not self._may_ask_for(rejection.scopes):
            logger.info('Upstream sign-in not offered: reason=beyond_scopes scopes=%s', list(rejection.scopes))
            return None
        scopes = list(
            dict.fromkeys(
                [
                    *self.select_next_sign_in_scopes(mcp_access_token.scopes),
                    *attachment.upstream_scopes,
                    *rejection.scopes,
                ]
            )
        )
        reauthorization_id = secrets.token_urlsafe(32)
        pending_reauthorization = PendingReauthorization(
            reauthorization_id=reauthorization_id,
            access_token=mcp_access_token.token,
            browser_id=attachment.browser_id,
            scopes=scopes,
            origin=self.tokens.origin,
        )
        await self._store.set(
            UPSTREAM_REAUTHORIZATION_NAMESPACE,
            reauthorization_id,
            pending_reauthorization.model_dump(),
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        await self._store.set_mapping(
            'mcp_access_token',
            mcp_access_token.token,
            UPSTREAM_REAUTHORIZATION_NAMESPACE,
            reauthorization_id,
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        logger.info('Upstream sign-in offered: reason=%s scopes=%s', rejection.error, scopes)
        return UpstreamReauthorization(
            reauthorization_id=reauthorization_id,
            url=f'{self._reauthorize_url}?{urllib.parse.urlencode({"id": reauthorization_id})}',
            message=f'Sign in to {self.upstream.host} again so this call can go on.',
        )

    async def wait_for_completion(self, timeout: float) -> bool:
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return False
        reauthorization_id = await self._store.get_mapping(
            'mcp_access_token', mcp_access_token.token, UPSTREAM_REAUTHORIZATION_NAMESPACE
        )
        if not reauthorization_id:
            return False
        with anyio.move_on_after(timeout):
            while True:
                data = await self._store.get(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
                if not self.tokens.is_from_here(data):
                    return False
                if data.get('completed'):
                    return True
                await anyio.sleep(_REAUTHORIZATION_POLL_INTERVAL)
        return False

    def _may_ask_for(self, scopes: tuple[str, ...]) -> bool:
        """Whether a sign-in may ask the upstream for ``scopes``, which ``auth.upstream.scopes`` caps when set."""
        if not scopes or not self.upstream.scopes:
            return True
        ceiling = [*self.upstream.required_scopes, *self.upstream.scopes]
        return set(scopes) <= self.scope_registry.upstream_policy.held_scopes(ceiling)

    # The page that starts a new upstream sign-in, and the upstream's redirect back from it

    async def begin_reauthorization(self, reauthorization_id: str, browser_id: str | None) -> str:
        """Send the browser to the upstream for the sign-in ``reauthorization_id`` names, under a fresh ``state``."""
        pending_reauthorization = await self._pending_reauthorization(reauthorization_id, browser_id)
        state = secrets.token_urlsafe(32)
        await self._store.set(
            UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE,
            state,
            {'reauthorization_id': reauthorization_id},
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        return self.upstream.build_authorize_url(state, pending_reauthorization.scopes)

    async def is_reauthorization(self, state: str) -> bool:
        """Whether the upstream's callback under ``state`` finishes a new sign-in for a token already held."""
        return await self._store.get(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state) is not None

    async def handle_reauthorization_callback(
        self, code: str, state: str, browser_id: str | None, iss: str | None = None
    ) -> None:
        """Redeem the upstream ``code`` and put the new upstream token behind the MCP token the sign-in was for."""
        reauthorization_id = await self._reauthorization_under(state)
        pending_reauthorization = await self._pending_reauthorization(reauthorization_id, browser_id)
        self.upstream.validate_issuer(iss)

        upstream_token = await self.upstream.exchange_code(code)
        attachment = McpTokenAttachment(
            upstream_scopes=upstream_token.resolve_granted_scopes(pending_reauthorization.scopes),
            links=_build_upstream_links(upstream_token.access_token, upstream_token.refresh_token),
            links_expire_at=_expiry_of(upstream_token.expires_in),
            browser_id=pending_reauthorization.browser_id,
        )
        await self._store.delete(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        if not await self.tokens.replace_attachment(pending_reauthorization.access_token, attachment):
            await self._store.delete(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
            raise HTTPException(400, 'The MCP sign-in this was for has ended')
        completed = pending_reauthorization.model_copy(update={'completed': True})
        await self._store.set(
            UPSTREAM_REAUTHORIZATION_NAMESPACE,
            reauthorization_id,
            completed.model_dump(),
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        logger.info('Upstream signed in again: expires_in=%s', upstream_token.expires_in)

    async def handle_reauthorization_error(self, error: str, state: str, browser_id: str | None) -> None:
        """End a new sign-in the upstream refused, so the call waiting on it stops waiting."""
        reauthorization_id = await self._reauthorization_under(state)
        await self._pending_reauthorization(reauthorization_id, browser_id)
        await self._store.delete(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        await self._store.delete(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
        logger.info('Upstream OAuth refused the new sign-in: error=%s', error)
        raise HTTPException(400, f'The sign-in service answered {error}')

    async def _reauthorization_under(self, state: str) -> str:
        data = await self._store.get(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        if not data:
            raise HTTPException(400, 'This sign-in has expired or was already finished')
        return data['reauthorization_id']

    async def _pending_reauthorization(self, reauthorization_id: str, browser_id: str | None) -> PendingReauthorization:
        """The new sign-in ``reauthorization_id`` names, which only the browser behind its token may run.

        Raises ``HTTPException`` when it is unknown, finished, or opened in another browser.
        """
        data = await self._store.get(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
        if not self.tokens.is_from_here(data) or data.get('completed'):
            logger.warning('Upstream sign-in rejected: reason=invalid_reauthorization')
            raise HTTPException(400, 'This sign-in has expired or was already finished')
        pending_reauthorization = PendingReauthorization.model_validate(data)
        if browser_id is None or pending_reauthorization.browser_id != browser_id:
            logger.warning('Upstream sign-in rejected: reason=other_browser')
            raise HTTPException(
                403, 'Open this link in the browser you first signed in with, so the gateway knows it is you'
            )
        return pending_reauthorization

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
        pending_authorization = await self._pending_upstream_authorization(state, browser_id)
        self.upstream.validate_issuer(iss)

        upstream_token = await self.upstream.exchange_code(code)
        redirect_uri = await self.tokens.complete_authorization(
            pending_authorization,
            McpTokenAttachment(
                upstream_scopes=upstream_token.resolve_granted_scopes(pending_authorization.next_sign_in_scopes),
                links=_build_upstream_links(upstream_token.access_token, upstream_token.refresh_token),
                links_expire_at=_expiry_of(upstream_token.expires_in),
                browser_id=pending_authorization.browser_id,
            ),
        )
        await self._store.delete(UPSTREAM_AUTH_STATE_NAMESPACE, state)

        logger.info(
            'Upstream OAuth callback handled: client_id=%s expires_in=%s refresh=%s',
            pending_authorization.client_id,
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
        pending_authorization = await self._pending_upstream_authorization(state, browser_id)
        await self._store.delete(UPSTREAM_AUTH_STATE_NAMESPACE, state)
        logger.info('Upstream OAuth refused the sign-in: client_id=%s error=%s', pending_authorization.client_id, error)
        forwarded = error if error in _UPSTREAM_FORWARDED_ERRORS else 'server_error'
        return construct_redirect_uri(
            pending_authorization.redirect_uri,
            error=forwarded,
            error_description=error_description or f'The upstream authorization server answered {error}',
            state=pending_authorization.client_state,
            iss=self.tokens.issuer,
        )

    async def _pending_upstream_authorization(self, state: str, browser_id: str | None) -> PendingAuthorization:
        """The sign-in the upstream's callback under ``state`` finishes, which ``browser_id`` must have approved.

        Raises ``HTTPException`` when ``state`` names none of this server's sign-ins against the current upstream,
        including one recorded before sign-ins were tied to a browser, or when another browser delivers it.
        A sign-in recorded against another upstream is refused outright,
        since its code would otherwise be redeemed at a token endpoint that never issued it.
        """
        pending_authorization = await self.tokens.load_pending_authorization(UPSTREAM_AUTH_STATE_NAMESPACE, state)
        if pending_authorization is None:
            logger.warning('OAuth callback rejected: reason=invalid_state')
            raise HTTPException(400, 'This sign-in has expired or was already finished')
        if browser_id is None or pending_authorization.browser_id != browser_id:
            logger.warning(
                'OAuth callback rejected: reason=other_browser client_id=%s', pending_authorization.client_id
            )
            raise HTTPException(400, 'This sign-in was started in another browser')
        return pending_authorization


class AuthorizationCodeCallbackRoutes(AuthRoutes):
    """The page the upstream sends the browser back to, which finishes the sign-in ``AuthorizationCodeBridge`` began.

    The browser cookie ties the callback to the browser that approved the consent page.
    """

    def __init__(self, bridge: AuthorizationCodeBridge, cookie: BrowserCookie) -> None:
        self._bridge = bridge
        self._cookie = cookie

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(CALLBACK_PATH, methods=['GET'])(self.finish_upstream)
        mcp.custom_route(REAUTHORIZE_PATH, methods=['GET'])(self.start_reauthorization)

    async def start_reauthorization(self, request: Request) -> Response:
        """Open the new upstream sign-in a client asked the user to start, in the browser that ran the first one."""
        reauthorization_id = request.query_params.get('id')
        if not reauthorization_id:
            return render_error_page('This link names no sign-in.', 400)
        try:
            redirect_uri = await self._bridge.begin_reauthorization(reauthorization_id, self._cookie.read(request))
        except HTTPException as exc:
            return render_error_page(exc.detail, exc.status_code)
        return redirect_without_cache(redirect_uri, 302)

    async def finish_upstream(self, request: Request) -> Response:
        """Take the upstream's redirect, and send the browser on to the MCP client with its code or the refusal.

        A failure the client cannot be told about is shown on the gateway's error page.
        """
        query = request.query_params
        state = query.get('state')
        browser_id = self._cookie.read(request)
        if state and await self._bridge.is_reauthorization(state):
            return await self._finish_reauthorization(request, state, browser_id)
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

    async def _finish_reauthorization(self, request: Request, state: str, browser_id: str | None) -> Response:
        """Finish a new sign-in for a token already held, which ends on a page rather than at the client."""
        query = request.query_params
        try:
            if error := query.get('error'):
                await self._bridge.handle_reauthorization_error(error, state, browser_id)
            elif code := query.get('code'):
                await self._bridge.handle_reauthorization_callback(code, state, browser_id, query.get('iss'))
            else:
                return render_error_page(_INCOMPLETE_CALLBACK, 400)
        except HTTPException as exc:
            return render_error_page(exc.detail, exc.status_code)
        return render_signed_in_again_page(self._bridge.upstream.host)


class AuthorizationCodeFlowHandler(OAuthFlowHandler, McpSettingsReader):
    """Assemble the per-user ``authorization_code`` flow, whose two directions are bound.

    It builds the gateway's MCP authorization server, the gateway's upstream OAuth client,
    and the ``AuthorizationCodeBridge`` that joins them.
    """

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'user'

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return frozenset(
            {'required_scopes', 'operation_scopes', 'clients', 'other_clients', 'access_token_ttl', 'refresh_token_ttl'}
        )

    @property
    def mcp_settings_requirement(self) -> str:
        return 'auth.upstream.flow authorization_code, where users sign in to the gateway'

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

        server_url_base = f'{flow_context.gateway_url.rstrip("/")}{flow_context.mount_path.rstrip("/")}'
        upstream = UpstreamAuthorizationCodeClient(
            authorization_url=oauth_flow.authorization_url,
            token_url=oauth_flow.token_url,
            client_id=client_id,
            client_secret=client_secret,
            callback_url=f'{server_url_base}{CALLBACK_PATH}',
            scopes=list(upstream_config.scopes),
            required_scopes=list(upstream_config.required_scopes),
            scope_descriptions=oauth_flow.scope_descriptions,
            audience_params=upstream_config.resolve_audience_params(),
            issuer=upstream_config.resolve_issuer(),
        )

        policy = build_mcp_token_policy(mcp_config)
        mcp_endpoint_scopes = policy.scopes
        # The same string the metadata publishes as ``issuer``,
        # since RFC 9207 compares the two without any normalisation.
        issuer = flow_context.issuer_url
        server_url = pydantic.AnyHttpUrl(issuer)
        clients = _build_client_directory(flow_context, mcp_endpoint_scopes, issuer)
        server, bridge = build_authorization_code_server(
            flow_context.store,
            issuer,
            upstream,
            flow_context.scope_registry,
            policy,
            clients,
            entry.name,
            flow_context.mcp_resource_url,
            f'{server_url_base}{REAUTHORIZE_PATH}',
        )
        # One cookie for both pages, since the callback must read what the consent page set.
        cookie = BrowserCookie(secure=issuer.startswith('https://'))
        settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=pydantic.AnyHttpUrl(flow_context.mcp_resource_url),
            validate_token_resource=True,
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
            reauthorizer=bridge,
            provider=server,
            settings=settings,
            mcp_tokens_carry_upstream_grant=True,
            authorization_server_metadata=_build_authorization_server_metadata(settings, clients),
            routes=[
                McpAuthorizationCodeRoutes(server, flow_context.scope_registry.mcp_policy, cookie),
                AuthorizationCodeCallbackRoutes(bridge, cookie),
            ],
            mcp_summary='gateway (authorization_code)',
        )


def build_authorization_code_server(
    store: TokenStore,
    issuer: str,
    upstream: UpstreamAuthorizationCodeClient,
    scope_registry: ScopeRegistry,
    policy: McpTokenPolicy,
    clients: McpClientDirectory,
    server_name: str,
    resource: str,
    reauthorize_url: str,
) -> tuple[McpAuthorizationCodeServer, AuthorizationCodeBridge]:
    """Build the gateway's MCP authorization server for one server, joined to ``upstream`` by the returned Bridge.

    ``scope_registry`` chooses what each sign-in asks the upstream for.
    ``reauthorize_url`` is where the user starts a new upstream sign-in for a token already held.

    ``issuer`` is the server's own issuer identifier, and every record is tied to it and to the upstream's server.
    ``resource`` is the canonical URI of the server's MCP endpoint, which every token is issued for.
    """
    tokens = McpTokenIssuer(
        store=store,
        origin=McpRecordOrigin(issuer=issuer, next_sign_in_server=upstream.authorization_server),
        policy=policy,
        resource=resource,
    )
    bridge = AuthorizationCodeBridge(tokens, upstream, scope_registry, store, reauthorize_url)
    return McpAuthorizationCodeServer(tokens, clients, bridge, server_name=server_name), bridge


def _build_authorization_server_metadata(settings: AuthSettings, clients: McpClientDirectory) -> OAuthMetadata:
    """The RFC 8414 document of the gateway's authorization server, as the SDK builds it plus what it adds.

    The server sends ``iss`` on its authorization responses,
    so RFC 9207 §2.3 has the document announce ``authorization_response_iss_parameter_supported``.
    That flag is what lets a client refuse a response that arrives without one.
    ``client_id_metadata_document_supported`` tells a client it may sign in with its metadata document's URL,
    which the MCP spec has it prefer to registering, and which the SDK does not announce yet.
    """
    metadata = build_metadata(
        issuer_url=settings.issuer_url,
        service_documentation_url=settings.service_documentation_url,
        client_registration_options=settings.client_registration_options or ClientRegistrationOptions(),
        revocation_options=settings.revocation_options or RevocationOptions(),
    )
    metadata.authorization_response_iss_parameter_supported = True
    if clients.accepts_client_metadata_documents:
        metadata.client_id_metadata_document_supported = True
    return metadata


def _build_client_directory(
    flow_context: OAuthFlowContext, mcp_endpoint_scopes: list[str], issuer: str
) -> McpClientDirectory:
    """The MCP clients this server admits.

    They are the clients ``auth.mcp.clients`` declares,
    with the Client ID Metadata Document clients ``other_clients`` admits.
    Where the config names neither, clients register themselves by Dynamic Client Registration.
    """
    mcp_config = flow_context.entry.auth.mcp
    directories: list[McpClientDirectory] = [
        build_mcp_client_directory(mcp_config.clients, mcp_endpoint_scopes),
        build_mcp_client_metadata_directory(mcp_config, mcp_endpoint_scopes, flow_context.client_metadata_resolver),
    ]
    if mcp_config.accepts_dynamic_registration:
        logger.warning(
            'Server "%s" accepts Dynamic Client Registration, which the MCP 2026-07-28 spec deprecates. '
            'Setting auth.mcp.clients or auth.mcp.other_clients turns it off.',
            flow_context.entry.name,
        )
        directories.append(McpDynamicClientRegistry(flow_context.store, issuer))
    return McpClientDirectoryChain(directories)


def _expiry_of(expires_in: int | None) -> int | None:
    """When a token lasting ``expires_in`` seconds from now stops working, or ``None`` for one that never does."""
    return None if expires_in is None else int(time.time()) + expires_in


def _renewed_attachment(
    attachment: McpTokenAttachment, refreshed: UpstreamTokenResponse, upstream_refresh_token: str
) -> McpTokenAttachment:
    """``attachment`` after the upstream refreshed its token, keeping the refresh token when no new one came."""
    return McpTokenAttachment(
        upstream_scopes=refreshed.resolve_granted_scopes(attachment.upstream_scopes),
        links=_build_upstream_links(refreshed.access_token, refreshed.refresh_token or upstream_refresh_token),
        links_expire_at=_expiry_of(refreshed.expires_in),
        browser_id=attachment.browser_id,
    )


def _build_upstream_links(access_token: str, refresh_token: str | None) -> dict[str, str]:
    """The links an MCP token carries to the upstream tokens behind it."""
    links = {UPSTREAM_ACCESS_TOKEN_LINK: access_token}
    if refresh_token:
        links[UPSTREAM_REFRESH_TOKEN_LINK] = refresh_token
    return links
