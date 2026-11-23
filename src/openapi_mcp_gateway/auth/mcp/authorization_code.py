import abc
import dataclasses
import logging
import secrets
import urllib.parse

from mcp.server import MCPServer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import Response

from ...scopes import McpAccessToken, McpScopePolicy
from ..routes import AuthRoutes
from .client import McpClientDirectory
from .client_id import client_metadata_host
from .consent import PENDING_AUTHORIZATION_TTL, ApprovedScopes, BrowserCookie, ConsentRegistry, PendingAuthorization
from .consent_page import ConsentPrompt, DescribedScope, redirect_without_cache, render_consent_page, render_error_page
from .token_issuer import McpTokenAttachment, McpTokenIssuer, require_client_id


logger = logging.getLogger(__name__)


# Where each server's consent page sits, under the server's own path.
CONSENT_PATH = '/auth/consent'

# Why a consent page cannot go on, as the user reads it.
_UNKNOWN_SIGN_IN = 'This sign-in has expired, was already finished, or was opened in another browser.'
_TAMPERED_FORM = 'The approval form was not the one this gateway sent.'


@dataclasses.dataclass(frozen=True)
class McpNextSignIn:
    """The sign-in a user goes on to after approving the gateway's consent page, as that page describes it.

    ``host`` is where it happens.
    ``scope_descriptions`` maps each scope it may ask for there to what it allows, when anything says so.
    """

    host: str
    scope_descriptions: dict[str, str] = dataclasses.field(default_factory=dict)


class McpAuthorizationCodeContinuation(abc.ABC):
    """What follows the gateway's own consent in an ``authorization_code`` sign-in.

    ``McpAuthorizationCodeServer`` calls it wherever its part of a sign-in hands over,
    so the server never needs to know what stands behind the MCP tokens it mints.
    """

    @abc.abstractmethod
    def describe_next_sign_in(self) -> McpNextSignIn | None:
        """Describe the sign-in that approval leads to, for the consent page, or return ``None`` when none follows."""

    def select_next_sign_in_scopes(self, mcp_scopes: list[str]) -> list[str]:
        """The scopes the sign-in after consent asks for, when the gateway is to grant ``mcp_scopes``.

        A browser's approval covers them too. None by default, as when no sign-in follows.
        """
        return []

    @abc.abstractmethod
    async def continue_after_consent(self, pending_authorization: PendingAuthorization) -> str:
        """Take over ``pending_authorization`` once the browser may go on, and return the URL to send it to."""

    async def refresh_attachment(self, client_id: str, attachment: McpTokenAttachment) -> McpTokenAttachment:
        """Return what tokens refreshed from ones carrying ``attachment`` carry, which by default is the same."""
        return attachment


class McpAuthorizationCodeServer:
    """The gateway's MCP authorization server for one server, the SDK's ``OAuthAuthorizationServerProvider``.

    It registers MCP clients, shows the gateway's consent page, and mints and rotates the ``mcp_...`` tokens.
    What happens after consent, and what each token is tied to, is left to ``continuation``.

    Every sign-in passes the consent page at ``CONSENT_PATH`` before it goes on,
    which is what the MCP spec asks of a proxy holding one static ``client_id`` at the upstream.
    The upstream's own consent names the gateway, never the MCP client behind it,
    and may be skipped once given, so without this page any registered client could collect a code silently.

    ``clients`` is where the clients allowed to sign in are found.
    """

    def __init__(
        self,
        tokens: McpTokenIssuer,
        clients: McpClientDirectory,
        continuation: McpAuthorizationCodeContinuation,
        server_name: str = 'gateway',
    ) -> None:
        self.tokens = tokens
        self.clients = clients
        self.continuation = continuation
        self._consents = ConsentRegistry(tokens.store, tokens.issuer)
        self._server_name = server_name

    @property
    def issuer(self) -> str:
        return self.tokens.issuer

    # MCP SDK OAuthAuthorizationServerProvider interface

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.clients.register(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Record the sign-in and send the browser to the consent page, the first place the gateway sees the browser.

        The SDK hands this method no request, so whether the browser already approved the client is decided there.
        A ``resource`` other than this endpoint is ``invalid_target``, as RFC 8707 §2 has it.
        That redirect names this authorization server per RFC 9207, as every other one from here does.
        """
        if not self.tokens.serves_resource(params.resource):
            return construct_redirect_uri(
                str(params.redirect_uri),
                error='invalid_target',
                error_description=f'Tokens here are for {self.tokens.resource}',
                state=params.state,
                iss=self.issuer,
            )
        # The scopes the SDK settled on for this client, always with the endpoint scopes every request must hold.
        scopes: list[str] = list(dict.fromkeys([*self.tokens.policy.scopes, *(params.scopes or [])]))
        pending_authorization = PendingAuthorization(
            request_id=secrets.token_urlsafe(32),
            client_id=require_client_id(client),
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=scopes,
            next_sign_in_scopes=self.continuation.select_next_sign_in_scopes(scopes),
            client_state=params.state,
            csrf_token=secrets.token_urlsafe(32),
            origin=self.tokens.origin,
        )
        await self._store_pending_authorization(pending_authorization)
        query = urllib.parse.urlencode({'request': pending_authorization.request_id})
        return f'{self.issuer.rstrip("/")}{CONSENT_PATH}?{query}'

    async def claim_authorization(self, request_id: str, browser_id: str) -> PendingAuthorization | None:
        """The sign-in waiting under ``request_id``, bound to ``browser_id`` by the first browser to open it.

        ``None`` when it expired, belongs to another server, or another browser opened it first,
        so a consent link passed on to someone else is worthless to them.
        """
        pending_authorization = await self._load_pending_authorization(request_id)
        if pending_authorization is not None and pending_authorization.browser_id is None:
            pending_authorization = pending_authorization.model_copy(update={'browser_id': browser_id})
            await self._store_pending_authorization(pending_authorization)
        return self._opened_by(pending_authorization, browser_id)

    async def load_shown_authorization(self, request_id: str, browser_id: str) -> PendingAuthorization | None:
        """The sign-in waiting under ``request_id`` if ``browser_id`` opened it, or ``None`` for any other browser."""
        return self._opened_by(await self._load_pending_authorization(request_id), browser_id)

    async def requires_consent(self, pending_authorization: PendingAuthorization) -> bool:
        """Whether the browser behind ``pending_authorization`` has to approve its client before it goes on."""
        if not self.clients.requires_consent(pending_authorization.client_id):
            return False
        return not await self._consents.is_approved(
            pending_authorization.bound_browser_id,
            pending_authorization.client_id,
            pending_authorization.scopes,
            pending_authorization.next_sign_in_scopes,
        )

    async def build_consent_prompt(
        self, pending_authorization: PendingAuthorization, mcp_scope_policy: McpScopePolicy
    ) -> ConsentPrompt:
        """What the consent page shows for ``pending_authorization``, its MCP access read from ``mcp_scope_policy``.

        On a step-up, the scopes beyond what the browser approved before are marked new.
        Nothing is marked on a first approval, where every scope would be.
        """
        next_sign_in = self.continuation.describe_next_sign_in()
        next_sign_in_scopes = pending_authorization.next_sign_in_scopes
        next_sign_in_descriptions = next_sign_in.scope_descriptions if next_sign_in else {}
        client = await self.clients.get(pending_authorization.client_id)
        approved_scopes = await self._consents.load_approved_scopes(
            pending_authorization.bound_browser_id, pending_authorization.client_id
        )
        new_scopes = (
            approved_scopes.find_new_scopes(pending_authorization.scopes, next_sign_in_scopes)
            if approved_scopes
            else ApprovedScopes()
        )
        return ConsentPrompt(
            server_name=self._server_name,
            next_sign_in_host=next_sign_in.host if next_sign_in else None,
            client_id=pending_authorization.client_id,
            client_name=client.client_name if client else None,
            client_host=client_metadata_host(pending_authorization.client_id),
            redirect_uri=pending_authorization.redirect_uri,
            next_sign_in_scopes=[
                DescribedScope(scope, next_sign_in_descriptions.get(scope), scope in new_scopes.next_sign_in_scopes)
                for scope in next_sign_in_scopes
            ],
            mcp_scopes=[
                DescribedScope(scope, mcp_scope_policy.mcp_defined_scopes[scope], scope in new_scopes.mcp_scopes)
                for scope in pending_authorization.scopes
                if scope in mcp_scope_policy.mcp_defined_scopes
            ],
            opens_unscoped_tools=mcp_scope_policy.opens_unscoped_operations,
            csrf_token=pending_authorization.csrf_token,
        )

    async def approve(self, pending_authorization: PendingAuthorization) -> str:
        """Record that the browser approved the client, and return where the sign-in goes on."""
        await self._consents.approve(
            pending_authorization.bound_browser_id,
            pending_authorization.client_id,
            pending_authorization.scopes,
            pending_authorization.next_sign_in_scopes,
        )
        logger.info('OAuth consent approved: client_id=%s', pending_authorization.client_id)
        return await self.continue_after_consent(pending_authorization)

    async def deny(self, pending_authorization: PendingAuthorization) -> str:
        """End the sign-in, and return the redirect telling the client the user refused, per RFC 6749 §4.1.2.1."""
        await self.tokens.store.delete('mcp_auth_request', pending_authorization.request_id)
        logger.info('OAuth consent denied: client_id=%s', pending_authorization.client_id)
        return construct_redirect_uri(
            pending_authorization.redirect_uri,
            error='access_denied',
            error_description='The user denied the sign-in',
            state=pending_authorization.client_state,
            iss=self.issuer,
        )

    async def continue_after_consent(self, pending_authorization: PendingAuthorization) -> str:
        """Hand ``pending_authorization`` on past the consent page, and return where the browser goes next.

        The continuation records the sign-in first, so a failure there leaves the consent page able to retry it.
        """
        next_url = await self.continuation.continue_after_consent(pending_authorization)
        await self.tokens.store.delete('mcp_auth_request', pending_authorization.request_id)
        return next_url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = await self._load_record('mcp_auth_code', authorization_code, client)
        return AuthorizationCode(**data) if data else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Exchange an MCP auth code for MCP access and refresh tokens."""
        client_id = require_client_id(client)
        redeemed = await self.tokens.redeem_authorization_code(client_id, authorization_code.code)
        if redeemed is None:
            logger.warning('OAuth code exchange rejected: reason=invalid_code client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='Invalid authorization code')

        # Only the code's own links, written beside the code with the same TTL.
        # Falling back to whatever the client was last linked to, as this once did,
        # could hand one user's credential to another user of the same client_id.
        _, attachment = redeemed
        if attachment is None:
            logger.warning('OAuth code exchange rejected: reason=link_lost client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='A credential this code stands for is gone')
        return await self.tokens.issue_tokens(client, authorization_code.scopes, attachment)

    async def load_access_token(self, token: str) -> McpAccessToken | None:
        """The token, unless its client is no longer admitted, so removing a client revokes what it already holds.

        A refresh needs no such check, since the SDK authenticates the client before it loads the refresh token.
        """
        access_token = await self.tokens.load_access_token(token)
        if access_token is None or not await self.clients.admits(access_token.client_id):
            return None
        return access_token

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = await self._load_record('mcp_refresh_token', refresh_token, client)
        if not data:
            return None
        return RefreshToken(
            token=data['token'],
            client_id=data['client_id'],
            scopes=data['scopes'],
            expires_at=data.get('expires_at'),
            resource=data.get('resource'),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate MCP tokens, letting the continuation renew whatever they are tied to."""
        client_id = require_client_id(client)
        data = await self._load_record('mcp_refresh_token', refresh_token.token, client)
        if not data:
            logger.warning('OAuth refresh rejected: reason=invalid_refresh_token client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='Invalid refresh token')

        attachment = await self.tokens.load_attachment('mcp_refresh_token', refresh_token.token, data)
        if attachment is None:
            logger.warning('OAuth refresh rejected: reason=link_lost client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='A credential this token stands for is gone')
        attachment = await self.continuation.refresh_attachment(client_id, attachment)

        new_token = await self.tokens.issue_tokens(client, scopes or data.get('scopes', []), attachment)
        await self.tokens.revoke_refresh_token(refresh_token.token)
        return new_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Delete MCP tokens and their paired refresh/access mappings."""
        if isinstance(token, AccessToken):
            await self.tokens.revoke_access_token(token.token)
            kind = 'access'
        else:
            await self.tokens.revoke_refresh_token(token.token)
            kind = 'refresh'
        logger.info('Revoked MCP %s token (server=%s)', kind, self._server_name)

    async def exchange_identity_assertion(
        self,
        client: OAuthClientInformationFull,
        params: IdentityAssertionParams,
    ) -> OAuthToken:
        """Decline the SEP-990 JWT-bearer identity-assertion grant.

        The gateway brokers upstream credentials through the authorization-code flow only,
        so it advertises no support for exchanging an ID-JAG for an access token.
        """
        raise TokenError(
            error='unsupported_grant_type',
            error_description='The JWT bearer grant is not supported by this authorization server',
        )

    # Private helpers

    async def _load_record(self, namespace: str, key: str, client: OAuthClientInformationFull) -> dict | None:
        """The code or token stored under ``key`` when ``client`` owns it and it is bound to this server."""
        data = await self.tokens.store.get(namespace, key)
        if self.tokens.is_from_here(data) and data['client_id'] == require_client_id(client):
            return data
        return None

    async def _store_pending_authorization(self, pending_authorization: PendingAuthorization) -> None:
        """Keep ``pending_authorization`` under its request id until the browser leaves the consent page."""
        await self.tokens.store.set(
            'mcp_auth_request',
            pending_authorization.request_id,
            pending_authorization.model_dump(),
            ttl=PENDING_AUTHORIZATION_TTL,
        )

    async def _load_pending_authorization(self, request_id: str) -> PendingAuthorization | None:
        """The sign-in waiting at this server's consent page under ``request_id``."""
        return await self.tokens.load_pending_authorization('mcp_auth_request', request_id)

    @staticmethod
    def _opened_by(pending_authorization: PendingAuthorization | None, browser_id: str) -> PendingAuthorization | None:
        """``pending_authorization`` when ``browser_id`` is the browser it is bound to, else ``None``."""
        if pending_authorization is not None and pending_authorization.browser_id != browser_id:
            logger.warning('OAuth consent rejected: reason=other_browser client_id=%s', pending_authorization.client_id)
            return None
        return pending_authorization


class McpAuthorizationCodeRoutes(AuthRoutes):
    """The consent page a browser visits during an ``authorization_code`` sign-in, under each server's path.

    ``CONSENT_PATH`` shows the page and takes its decision.
    Each step reads ``cookie``, which ties the whole sign-in to the browser that approved it,
    so the step after consent must read the same one.
    ``mcp_scope_policy`` tells the page what each MCP scope allows, and which tools need none.
    """

    def __init__(
        self, server: McpAuthorizationCodeServer, mcp_scope_policy: McpScopePolicy, cookie: BrowserCookie
    ) -> None:
        self._server = server
        self._mcp_scope_policy = mcp_scope_policy
        self._cookie = cookie

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(CONSENT_PATH, methods=['GET'])(self.show_consent)
        mcp.custom_route(CONSENT_PATH, methods=['POST'])(self.submit_consent)

    async def show_consent(self, request: Request) -> Response:
        """Bind the sign-in to this browser, then ask for consent or go straight on.

        The page is skipped for a client the operator exempts, and for one this browser already approved.
        """
        browser_id = self._cookie.read_or_issue(request)
        pending_authorization = await self._server.claim_authorization(
            request.query_params.get('request', ''), browser_id
        )
        if pending_authorization is None:
            response: Response = render_error_page(_UNKNOWN_SIGN_IN, 400)
        elif await self._server.requires_consent(pending_authorization):
            response = render_consent_page(
                await self._server.build_consent_prompt(pending_authorization, self._mcp_scope_policy)
            )
        else:
            response = redirect_without_cache(await self._server.continue_after_consent(pending_authorization), 302)
        self._cookie.attach(response, browser_id)
        return response

    async def submit_consent(self, request: Request) -> Response:
        """Act on the decision the consent page posts, which counts only from the browser the page was shown to."""
        pending_authorization = await self._shown_authorization(request)
        if pending_authorization is None:
            return render_error_page(_UNKNOWN_SIGN_IN, 400)
        form = await request.form()
        if not secrets.compare_digest(str(form.get('csrf_token', '')), pending_authorization.csrf_token):
            logger.warning('OAuth consent rejected: reason=csrf_mismatch client_id=%s', pending_authorization.client_id)
            return render_error_page(_TAMPERED_FORM, 400)
        match form.get('decision'):
            case 'approve':
                redirect_uri = await self._server.approve(pending_authorization)
            case 'deny':
                redirect_uri = await self._server.deny(pending_authorization)
            case _:
                return render_error_page(_TAMPERED_FORM, 400)
        return redirect_without_cache(redirect_uri, 303)

    async def _shown_authorization(self, request: Request) -> PendingAuthorization | None:
        """The sign-in whose consent page this browser was shown, or ``None`` for any other browser."""
        browser_id = self._cookie.read(request)
        if browser_id is None:
            return None
        return await self._server.load_shown_authorization(request.query_params.get('request', ''), browser_id)
