import logging
import secrets
import time

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
    UpstreamSetting,
)
from ..mcp.authorization_code import (
    McpAuthorizationCodeContinuation,
    McpAuthorizationCodeRoutes,
    McpAuthorizationCodeServer,
    McpNextSignIn,
)
from ..mcp.client import McpClientDirectory, McpClientDirectoryChain, McpDynamicClientRegistry
from ..mcp.consent import PENDING_AUTHORIZATION_TTL, McpBrowserCookie, McpPendingAuthorization
from ..mcp.consent_page import redirect_without_cache, render_error_page, render_signed_in_again_page
from ..mcp.record_origin import McpRecordOrigin
from ..mcp.token_issuer import McpTokenAttachment, McpTokenIssuer, McpTokenPolicy
from ..settings_mapping import (
    build_mcp_client_directory,
    build_mcp_client_metadata_directory,
    build_mcp_token_policy,
)
from ..upstream.authorization_code import UpstreamAuthorizationCodeClient
from ..upstream.authorization_code_refresh import UpstreamTokenRefresher
from ..upstream.resolver import UpstreamTokenSourceAuthResolver
from ..upstream.token_source import UpstreamTokenSource
from .authorization_code_reauthorization import REAUTHORIZE_PATH, AuthorizationCodeReauthorizer
from .authorization_code_upstream_token import (
    UPSTREAM_ACCESS_TOKEN_LINK,
    UPSTREAM_REFRESH_TOKEN_LINK,
    build_upstream_links,
    expiry_of,
    renewed_attachment,
)


logger = logging.getLogger(__name__)


# Where the upstream sends the browser back, under the server's own path.
CALLBACK_PATH = '/auth/callback'

# Where a sign-in waits while the browser is at the upstream, keyed by the ``state`` sent there.
UPSTREAM_AUTH_STATE_NAMESPACE = 'upstream_auth_state'


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
    ``refresher`` renews the upstream token on an MCP refresh, once however many callers ask.
    ``scope_registry`` chooses what each sign-in asks the upstream for, from the MCP scopes it will grant.
    ``store`` keeps the sign-ins waiting at the upstream, which belong to neither side alone.
    """

    def __init__(
        self,
        tokens: McpTokenIssuer,
        upstream: UpstreamAuthorizationCodeClient,
        refresher: UpstreamTokenRefresher,
        scope_registry: ScopeRegistry,
        store: TokenStore,
    ) -> None:
        self.tokens = tokens
        self.upstream = upstream
        self.refresher = refresher
        self.scope_registry = scope_registry
        self._store = store

    # McpAuthorizationCodeContinuation

    def describe_next_sign_in(self) -> McpNextSignIn:
        return McpNextSignIn(host=self.upstream.host, scope_descriptions=self.upstream.scope_descriptions)

    def select_next_sign_in_scopes(self, mcp_scopes: list[str]) -> list[str]:
        return self.scope_registry.requested_upstream_scopes(
            self.upstream.scopes, self.upstream.required_scopes, mcp_scopes
        )

    async def continue_after_consent(self, pending_authorization: McpPendingAuthorization) -> str:
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
            refreshed = await self.refresher.refresh(upstream_refresh_token)
        except HTTPException as exception:
            if remaining > 0:
                return attachment
            logger.warning(
                'OAuth refresh rejected: reason=upstream_expired client_id=%s detail=%s', client_id, exception.detail
            )
            raise TokenError(
                error='invalid_grant',
                error_description='The upstream sign-in expired and could not be renewed; sign in again',
            ) from exception
        return renewed_attachment(attachment, refreshed, upstream_refresh_token)

    # UpstreamTokenSource

    async def get_token(self) -> str | None:
        """The upstream token behind the MCP access token of the call being handled."""
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return None
        return await self.tokens.load_access_token_link(mcp_access_token.token, UPSTREAM_ACCESS_TOKEN_LINK)

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
                links=build_upstream_links(upstream_token.access_token, upstream_token.refresh_token),
                links_expire_at=expiry_of(upstream_token.expires_in),
                browser_id=pending_authorization.browser_id,
                grant_id=secrets.token_urlsafe(32),
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

    async def _pending_upstream_authorization(self, state: str, browser_id: str | None) -> McpPendingAuthorization:
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
    """The pages the upstream sends the browser back to, and where a new upstream sign-in starts.

    The upstream knows one callback, so it finishes both a first sign-in ``AuthorizationCodeBridge`` began
    and a new one ``AuthorizationCodeReauthorizer`` began, told apart by the ``state`` each sent.
    The browser cookie ties each to the browser that started it.
    """

    def __init__(
        self, bridge: AuthorizationCodeBridge, reauthorizer: AuthorizationCodeReauthorizer, cookie: McpBrowserCookie
    ) -> None:
        self._bridge = bridge
        self._reauthorizer = reauthorizer
        self._cookie = cookie

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(CALLBACK_PATH, methods=['GET'])(self.finish_upstream)
        mcp.custom_route(REAUTHORIZE_PATH, methods=['GET'])(self.begin_sign_in)

    async def begin_sign_in(self, request: Request) -> Response:
        """Open the new upstream sign-in a client asked the user to start, in the browser that ran the first one."""
        reauthorization_id = request.query_params.get('id')
        if not reauthorization_id:
            return render_error_page('This link names no sign-in.', 400)
        try:
            redirect_uri = await self._reauthorizer.begin_sign_in(reauthorization_id, self._cookie.read(request))
        except HTTPException as exception:
            return render_error_page(exception.detail, exception.status_code)
        return redirect_without_cache(redirect_uri, 302)

    async def finish_upstream(self, request: Request) -> Response:
        """Take the upstream's redirect, and send the browser on to the MCP client with its code or the refusal.

        A failure the client cannot be told about is shown on the gateway's error page.
        """
        query = request.query_params
        state = query.get('state')
        browser_id = self._cookie.read(request)
        if state and await self._reauthorizer.is_sign_in_state(state):
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
        except HTTPException as exception:
            return render_error_page(exception.detail, exception.status_code)
        return redirect_without_cache(redirect_uri, 302)

    async def _finish_reauthorization(self, request: Request, state: str, browser_id: str | None) -> Response:
        """Finish a new sign-in for a token already held, which ends on a page rather than at the client."""
        query = request.query_params
        try:
            if error := query.get('error'):
                await self._reauthorizer.handle_upstream_error(error, state, browser_id)
            elif code := query.get('code'):
                await self._reauthorizer.handle_upstream_callback(code, state, browser_id, query.get('iss'))
            else:
                return render_error_page(_INCOMPLETE_CALLBACK, 400)
        except HTTPException as exception:
            return render_error_page(exception.detail, exception.status_code)
        return render_signed_in_again_page(self._reauthorizer.upstream.host)


class AuthorizationCodeFlowHandler(OAuthFlowHandler, McpSettingsReader):
    """Assemble the per-user ``authorization_code`` flow, whose two directions are bound.

    It builds the gateway's MCP authorization server, the gateway's upstream OAuth client,
    and the ``AuthorizationCodeBridge`` that joins them.
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
                'authorization_url',
                'token_url',
                'scopes',
                'required_scopes',
                'issuer',
                'resource',
                'audience',
                'enforce_scopes',
                'scope_implies',
            }
        )

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
        )
        reauthorizer = AuthorizationCodeReauthorizer(
            bridge.tokens,
            upstream,
            bridge.refresher,
            flow_context.scope_registry,
            f'{server_url_base}{REAUTHORIZE_PATH}',
        )
        # One cookie for both pages, since the callback must read what the consent page set.
        cookie = McpBrowserCookie(secure=issuer.startswith('https://'))
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
            reauthorizer=reauthorizer,
            provider=server,
            settings=settings,
            mcp_tokens_carry_upstream_grant=True,
            authorization_server_metadata=_build_authorization_server_metadata(settings, clients),
            routes=[
                McpAuthorizationCodeRoutes(server, flow_context.scope_registry.mcp_policy, cookie),
                AuthorizationCodeCallbackRoutes(bridge, reauthorizer, cookie),
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
) -> tuple[McpAuthorizationCodeServer, AuthorizationCodeBridge]:
    """Build the gateway's MCP authorization server for one server, joined to ``upstream`` by the returned Bridge.

    ``scope_registry`` chooses what each sign-in asks the upstream for.

    ``issuer`` is the server's own issuer identifier, and every record is tied to it and to the upstream's server.
    ``resource`` is the canonical URI of the server's MCP endpoint, which every token is issued for.
    """
    tokens = McpTokenIssuer(
        store=store,
        origin=McpRecordOrigin(issuer=issuer, next_sign_in_server=upstream.authorization_server),
        policy=policy,
        resource=resource,
    )
    bridge = AuthorizationCodeBridge(tokens, upstream, UpstreamTokenRefresher(upstream, store), scope_registry, store)
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
