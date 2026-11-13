import dataclasses
import logging
import secrets
import time
import typing
import urllib.parse

import httpx
import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.exceptions import HTTPException

from ...scopes import BrokeredAccessToken, McpScopePolicy
from ...stores.base import TokenStore
from ..resolver import AuthorizationCodeAuthResolver
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup
from .clients import ClientDirectory, DynamicClientRegistry, PreRegisteredClientDirectory
from .consent import PENDING_AUTHORIZATION_TTL, ConsentRegistry, PendingAuthorization
from .consent_page import ConsentPrompt, DescribedScope


logger = logging.getLogger(__name__)


# The scope the gateway issues and demands when it is the authorization server.
# It has no upstream meaning, since the gateway mints these tokens itself,
# and exists only so the OAuth machinery has a scope to name.
DEFAULT_MCP_SCOPE = 'api'

# Where each server's browser-facing pages sit, under the server's own path.
CONSENT_PATH = '/auth/consent'
CALLBACK_PATH = '/auth/callback'

# The upstream authorization errors that mean the same to the MCP client, and so reach it unchanged.
_FORWARDED_UPSTREAM_ERRORS = frozenset({'access_denied', 'temporarily_unavailable'})


@dataclasses.dataclass(frozen=True)
class UpstreamOAuthClient:
    """What the gateway needs to act as an OAuth client at the upstream authorization server.

    ``callback_url`` is the gateway's own address,
    and belongs here because it is only ever meaningful as the ``redirect_uri`` of this conversation.

    ``scope_descriptions`` maps each upstream scope to what it allows, as the spec's OAuth flow states it.

    ``issuer`` is the upstream authorization server's RFC 8414 issuer identifier.
    It is what an RFC 9207 ``iss`` on the authorization response is compared against,
    so without it a present ``iss`` cannot be checked at all.
    """

    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str
    callback_url: str
    scopes: list[str] = dataclasses.field(default_factory=list)
    scope_descriptions: dict[str, str] = dataclasses.field(default_factory=dict)
    audience_params: dict[str, str] = dataclasses.field(default_factory=dict)
    issuer: str | None = None

    @property
    def authorization_server(self) -> str:
        """Identify the upstream authorization server that persisted upstream tokens belong to.

        The issuer when one is configured, since that is the identifier the MCP spec keys credentials by.
        Otherwise the token endpoint, the one place every stored upstream token is redeemed or refreshed,
        so that pointing the gateway at another server still invalidates what the old one issued.
        """
        return self.issuer or self.token_url


@dataclasses.dataclass(frozen=True)
class UpstreamTokenResponse:
    """What the upstream token endpoint answered, with ``scope`` exactly as sent, or ``None`` when omitted."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str | None

    def granted_scopes(self, previous: list[str]) -> list[str]:
        """The scopes the upstream granted, read as RFC 6749 §5.1 states them.

        An omitted ``scope`` means the grant did not change, so it is ``previous``,
        which for a first grant is what the gateway requested.
        """
        return self.scope.split() if self.scope is not None else list(previous)


@dataclasses.dataclass(frozen=True)
class IssuedTokenPolicy:
    """What the gateway grants on the ``mcp_...`` tokens it mints for MCP clients.

    Separate from ``UpstreamOAuthClient`` because the two describe opposite directions,
    and both carry a ``scopes`` that would otherwise need a prefix to tell apart.
    """

    scopes: list[str] = dataclasses.field(default_factory=lambda: [DEFAULT_MCP_SCOPE])
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 86400


class AuthorizationCodeProvider:
    """MCP OAuth server provider that fronts an upstream ``authorization_code`` API.

    Registers MCP clients, forwards browser authorization to the upstream IdP,
    exchanges grants at the upstream token endpoint, and keeps the MCP-to-upstream token mappings inside ``store``.
    Each MCP access token corresponds to one user's upstream token.

    The upstream token is a separate credential from the ``mcp_...`` token handed to the MCP client,
    which is what the MCP authorization spec requires of a server calling an upstream API.
    ``upstream.audience_params`` names the API that token is for,
    for an upstream whose API and authorization server are different parties.

    ``issuer`` is the gateway's own issuer identifier for this server, the one its metadata publishes.
    Every record the provider persists is bound to it and to ``upstream.authorization_server``,
    because one store serves every server in the gateway and outlives configuration changes.
    Without that binding a client registered with one server's authorization server is honoured by another's,
    and a token minted for one server unlocks the upstream credential of the next,
    which is the cross-server credential reuse the MCP spec forbids.
    A record that names a different issuer or upstream, or none at all, is treated as absent,
    so the client re-registers or re-authorizes rather than failing outright.

    ``clients`` is where the clients allowed to sign in are found.
    It defaults to Dynamic Client Registration into ``store``.

    Every sign-in passes the gateway's consent page at ``CONSENT_PATH`` before it reaches the upstream,
    which is what the MCP spec asks of a proxy holding one static ``client_id`` at the upstream.
    The upstream's own consent names the gateway, never the MCP client behind it,
    and may be skipped once given, so without this page any registered client could collect a code silently.
    """

    def __init__(
        self,
        store: TokenStore,
        upstream: UpstreamOAuthClient,
        issued_tokens: IssuedTokenPolicy,
        issuer: str,
        prefix: str = 'gateway',
        clients: ClientDirectory | None = None,
    ) -> None:
        self.store = store
        self.upstream = upstream
        self.issued_tokens = issued_tokens
        self.issuer = issuer
        self.clients = clients or DynamicClientRegistry(store, issuer)
        self._consents = ConsentRegistry(store, issuer)
        self._prefix = prefix

    # MCP SDK OAuthAuthorizationServerProvider interface

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.clients.register(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Record the sign-in and send the browser to the consent page, the first place the gateway sees the browser.

        The SDK hands this method no request, so whether the browser already approved the client is decided there.
        The expected upstream issuer is recorded with the request it belongs to, as the MCP spec requires,
        so the callback checks the response against the server this request was sent to,
        even if configuration changed in between.
        """
        pending = PendingAuthorization(
            request_id=secrets.token_urlsafe(32),
            client_id=self._require_client_id(client),
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            # The scopes the SDK settled on for this client, always with the endpoint scopes every request must hold.
            scopes=list(dict.fromkeys([*self.issued_tokens.scopes, *(params.scopes or [])])),
            client_state=params.state,
            csrf_token=secrets.token_urlsafe(32),
            upstream_issuer=self.upstream.issuer,
            **self._binding(),
        )
        await self._store_pending(pending)
        return f'{self.issuer}{CONSENT_PATH}?{urllib.parse.urlencode({"request": pending.request_id})}'

    async def claim_authorization(self, request_id: str, browser_id: str) -> PendingAuthorization | None:
        """The sign-in waiting under ``request_id``, bound to ``browser_id`` by the first browser to open it.

        ``None`` when it expired, belongs to another server, or another browser opened it first,
        so a consent link passed on to someone else is worthless to them.
        """
        pending = await self._load_pending(request_id)
        if pending is not None and pending.browser_id is None:
            pending = pending.model_copy(update={'browser_id': browser_id})
            await self._store_pending(pending)
        return self._opened_by(pending, browser_id)

    async def shown_authorization(self, request_id: str, browser_id: str) -> PendingAuthorization | None:
        """The sign-in waiting under ``request_id`` if ``browser_id`` opened it, or ``None`` for any other browser."""
        return self._opened_by(await self._load_pending(request_id), browser_id)

    async def requires_consent(self, pending: PendingAuthorization) -> bool:
        """Whether the browser behind ``pending`` has to approve its client before the upstream sign-in."""
        if not self.clients.requires_consent(pending.client_id):
            return False
        return not await self._consents.is_approved(
            pending.bound_browser_id, pending.client_id, pending.scopes, self.upstream.scopes
        )

    async def consent_prompt(self, pending: PendingAuthorization, mcp_scope_policy: McpScopePolicy) -> ConsentPrompt:
        """What the consent page shows for ``pending``, its MCP access read from ``mcp_scope_policy``."""
        client = await self.clients.get(pending.client_id)
        return ConsentPrompt(
            server_name=self._prefix,
            upstream_host=urllib.parse.urlsplit(self.upstream.authorization_url).netloc,
            client_id=pending.client_id,
            client_name=client.client_name if client else None,
            redirect_uri=pending.redirect_uri,
            upstream_scopes=[
                DescribedScope(scope, self.upstream.scope_descriptions.get(scope)) for scope in self.upstream.scopes
            ],
            mcp_scopes=[
                DescribedScope(scope, mcp_scope_policy.defined_mcp_scopes[scope])
                for scope in pending.scopes
                if scope in mcp_scope_policy.defined_mcp_scopes
            ],
            opens_unscoped_tools=mcp_scope_policy.opens_unscoped_operations,
            csrf_token=pending.csrf_token,
        )

    async def approve(self, pending: PendingAuthorization) -> str:
        """Record that the browser approved the client, and return the upstream authorize URL."""
        await self._consents.approve(pending.bound_browser_id, pending.client_id, pending.scopes, self.upstream.scopes)
        logger.info('OAuth consent approved: client_id=%s', pending.client_id)
        return await self.continue_to_upstream(pending)

    async def deny(self, pending: PendingAuthorization) -> str:
        """End the sign-in, and return the redirect telling the client the user refused, per RFC 6749 §4.1.2.1."""
        await self.store.delete('mcp_auth_request', pending.request_id)
        logger.info('OAuth consent denied: client_id=%s', pending.client_id)
        return construct_redirect_uri(
            pending.redirect_uri,
            error='access_denied',
            error_description='The user denied the sign-in',
            state=pending.client_state,
            iss=self.issuer,
        )

    async def continue_to_upstream(self, pending: PendingAuthorization) -> str:
        """Return the upstream authorize URL, under a fresh ``state`` only the browser behind ``pending`` can finish.

        The client's own ``state`` is never sent upstream.
        It is kept with the sign-in and handed back to the client at the end, as RFC 6749 requires.
        """
        state = secrets.token_urlsafe(32)
        await self.store.set('mcp_auth_state', state, pending.model_dump(), ttl=PENDING_AUTHORIZATION_TTL)
        await self.store.delete('mcp_auth_request', pending.request_id)

        query_params = {
            'client_id': self.upstream.client_id,
            'redirect_uri': self.upstream.callback_url,
            'state': state,
            'response_type': 'code',
        }
        if self.upstream.scopes:
            query_params['scope'] = ' '.join(self.upstream.scopes)
        # Sent on the authorization request as well as the token request,
        # since an authorization server binds the audience at consent time,
        # and RFC 8707 §2 requires the parameter on both.
        query_params.update(self.upstream.audience_params)

        logger.info(
            'Upstream OAuth authorize: scopes=%s audience_params=%s',
            self.upstream.scopes,
            self.upstream.audience_params,
        )
        return f'{self.upstream.authorization_url}?{urllib.parse.urlencode(query_params)}'

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_auth_code', authorization_code)
        if data and data['client_id'] == client_id and self._is_bound_here(data):
            return AuthorizationCode(**data)
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Exchange an MCP auth code for MCP access and refresh tokens."""
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_auth_code', authorization_code.code)

        if not data or data['client_id'] != client_id or not self._is_bound_here(data):
            logger.warning('OAuth code exchange rejected: reason=invalid_code client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='Invalid authorization code')

        # Only the code's own mapping, written beside the code with the same TTL.
        # Falling back to the client's most recent upstream token, as this once did,
        # could hand one user's upstream credential to another user of the same client_id.
        api_access_token = await self.store.get_mapping('mcp_auth_code', authorization_code.code, 'api_access_token')

        if not api_access_token:
            logger.warning('OAuth code exchange rejected: reason=no_upstream_token client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='No upstream API token found')

        api_refresh_token = await self.store.get_mapping('mcp_auth_code', authorization_code.code, 'api_refresh_token')

        return await self._issue_mcp_token(
            client_id=client_id,
            scopes=authorization_code.scopes,
            upstream_scopes=self._recorded_upstream_scopes(data),
            api_access_token=api_access_token,
            api_refresh_token=api_refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = await self.store.get('mcp_access_token', token)
        if data and self._is_bound_here(data):
            return BrokeredAccessToken(**{**data, 'upstream_scopes': self._recorded_upstream_scopes(data)})
        return None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_refresh_token', refresh_token)
        if data and data['client_id'] == client_id and self._is_bound_here(data):
            return RefreshToken(**data)
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate MCP tokens while preserving upstream refresh semantics."""
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_refresh_token', refresh_token.token)

        if not data or data['client_id'] != client_id or not self._is_bound_here(data):
            logger.warning('OAuth refresh rejected: reason=invalid_refresh_token client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='Invalid refresh token')

        api_access_token = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'api_access_token')
        api_refresh_token = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'api_refresh_token')

        if not api_access_token:
            logger.warning('OAuth refresh rejected: reason=mapping_lost client_id=%s', client_id)
            raise TokenError(error='invalid_grant', error_description='Upstream token mapping lost')

        upstream_scopes = self._recorded_upstream_scopes(data)
        if not await self.store.get('api_access_token', api_access_token):
            if not api_refresh_token:
                logger.warning('OAuth refresh rejected: reason=upstream_expired_no_refresh client_id=%s', client_id)
                raise TokenError(
                    error='invalid_grant',
                    error_description='Upstream token expired and no refresh token available; re-authenticate',
                )
            refreshed = await self._request_upstream_token(
                {
                    'client_id': self.upstream.client_id,
                    'client_secret': self.upstream.client_secret,
                    'refresh_token': api_refresh_token,
                    'grant_type': 'refresh_token',
                }
            )
            api_access_token = refreshed.access_token
            api_refresh_token = refreshed.refresh_token or api_refresh_token
            upstream_scopes = refreshed.granted_scopes(upstream_scopes)
            await self._store_api_token(client_id, api_access_token, refreshed.expires_in)

        new_token = await self._issue_mcp_token(
            client_id=client_id,
            scopes=scopes or data.get('scopes', []),
            upstream_scopes=upstream_scopes,
            api_access_token=api_access_token,
            api_refresh_token=api_refresh_token,
        )

        old_access = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'mcp_access_token')
        if old_access:
            await self.store.delete('mcp_access_token', old_access)
        await self.store.delete('mcp_refresh_token', refresh_token.token)

        return new_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Delete MCP tokens and their paired refresh/access mappings."""
        kind = 'access' if isinstance(token, AccessToken) else 'refresh'
        if isinstance(token, AccessToken):
            paired = await self.store.get_mapping('mcp_access_token', token.token, 'mcp_refresh_token')
            if paired:
                await self.store.delete('mcp_refresh_token', paired)
            await self.store.delete('mcp_access_token', token.token)
        elif isinstance(token, RefreshToken):
            paired = await self.store.get_mapping('mcp_refresh_token', token.token, 'mcp_access_token')
            if paired:
                await self.store.delete('mcp_access_token', paired)
            await self.store.delete('mcp_refresh_token', token.token)
        logger.info('Revoked MCP %s token (prefix=%s)', kind, self._prefix)

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

    # Gateway-specific methods

    async def handle_upstream_callback(
        self, code: str, state: str, browser_id: str | None, iss: str | None = None
    ) -> str:
        """Finish the browser redirect by swapping the upstream ``code`` for MCP auth artefacts.

        Validates ``state`` against the browser that approved the sign-in and the RFC 9207 ``iss``,
        exchanges tokens at the upstream token endpoint,
        persists the upstream credentials, builds an MCP authorization code, and returns the client redirect URI.
        The checks run before the code leaves the gateway,
        since a mix-up attack succeeds the moment the code reaches the wrong token endpoint.
        """
        pending = await self._pending_upstream(state, browser_id)
        self._validate_upstream_response_issuer(pending, iss)

        upstream_token = await self._request_upstream_token(
            {
                'client_id': self.upstream.client_id,
                'client_secret': self.upstream.client_secret,
                'code': code,
                'redirect_uri': self.upstream.callback_url,
                'grant_type': 'authorization_code',
            }
        )
        mcp_auth_code = f'mcp_{secrets.token_hex(16)}'
        await self.store.set(
            'mcp_auth_code',
            mcp_auth_code,
            {
                'code': mcp_auth_code,
                'client_id': pending.client_id,
                'redirect_uri': pending.redirect_uri,
                'redirect_uri_provided_explicitly': pending.redirect_uri_provided_explicitly,
                'expires_at': time.time() + 300,
                'scopes': pending.scopes,
                'code_challenge': pending.code_challenge,
                'upstream_scopes': upstream_token.granted_scopes(self.upstream.scopes),
                **self._binding(),
            },
            ttl=300,
        )

        await self._store_api_token(pending.client_id, upstream_token.access_token, upstream_token.expires_in)

        await self.store.set_mapping(
            'mcp_auth_code', mcp_auth_code, 'api_access_token', upstream_token.access_token, ttl=300
        )
        if upstream_token.refresh_token:
            await self.store.set_mapping(
                'mcp_auth_code', mcp_auth_code, 'api_refresh_token', upstream_token.refresh_token, ttl=300
            )

        await self.store.delete('mcp_auth_state', state)

        logger.info(
            'Upstream OAuth callback handled: client_id=%s expires_in=%s refresh=%s',
            pending.client_id,
            upstream_token.expires_in,
            bool(upstream_token.refresh_token),
        )
        # Per RFC 9207, name this authorization server in its own response, as its metadata advertises,
        # so an MCP client talking to several authorization servers can tell which one answered.
        return construct_redirect_uri(
            pending.redirect_uri, code=mcp_auth_code, state=pending.client_state, iss=self.issuer
        )

    async def handle_upstream_error(
        self, error: str, state: str, browser_id: str | None, error_description: str | None = None
    ) -> str:
        """End a sign-in the upstream refused, and return the redirect telling the client, per RFC 6749 §4.1.2.1.

        ``access_denied`` and ``temporarily_unavailable`` mean the same to the client and pass through.
        Any other error concerns the gateway's own request to the upstream, so the client hears ``server_error``.
        """
        pending = await self._pending_upstream(state, browser_id)
        await self.store.delete('mcp_auth_state', state)
        logger.info('Upstream OAuth refused the sign-in: client_id=%s error=%s', pending.client_id, error)
        forwarded = error if error in _FORWARDED_UPSTREAM_ERRORS else 'server_error'
        return construct_redirect_uri(
            pending.redirect_uri,
            error=forwarded,
            error_description=error_description or f'The upstream authorization server answered {error}',
            state=pending.client_state,
            iss=self.issuer,
        )

    async def get_api_access_token(self) -> str | None:
        """Map the active MCP access token (from request context) to the upstream bearer."""
        mcp_access_token = get_access_token()
        if not mcp_access_token:
            return None
        return await self.store.get_mapping('mcp_access_token', mcp_access_token.token, 'api_access_token')

    # Private helpers

    @staticmethod
    def _require_client_id(client: OAuthClientInformationFull) -> str:
        if not client.client_id:
            raise ValueError('client_id is required')
        return client.client_id

    def _binding(self) -> dict[str, str]:
        """The fields that tie a persisted code or token to this issuer and this upstream."""
        return {'issuer': self.issuer, 'upstream_authorization_server': self.upstream.authorization_server}

    def _is_bound_here(self, data: dict[str, typing.Any]) -> bool:
        """Whether a stored code or token was minted by this server against the current upstream.

        A record from before binding existed carries neither field and so never matches.
        Accepting it would leave the cross-server hole open for as long as such records live.
        """
        return all(data.get(field) == value for field, value in self._binding().items())

    def _recorded_upstream_scopes(self, data: dict[str, typing.Any]) -> list[str]:
        """The upstream scopes a stored code or token records.

        One stored before the grant was recorded is taken to hold what the gateway requests,
        as an omitted ``scope`` is.
        """
        return data.get('upstream_scopes', list(self.upstream.scopes))

    async def _store_pending(self, pending: PendingAuthorization) -> None:
        """Keep ``pending`` under its request id until the browser leaves for the upstream."""
        await self.store.set(
            'mcp_auth_request', pending.request_id, pending.model_dump(), ttl=PENDING_AUTHORIZATION_TTL
        )

    async def _load_pending(self, request_id: str) -> PendingAuthorization | None:
        """The sign-in waiting at this server's consent page under ``request_id``."""
        data = await self.store.get('mcp_auth_request', request_id)
        return PendingAuthorization.model_validate(data) if data and self._is_bound_here(data) else None

    @staticmethod
    def _opened_by(pending: PendingAuthorization | None, browser_id: str) -> PendingAuthorization | None:
        """``pending`` when ``browser_id`` is the browser it is bound to, else ``None``."""
        if pending is not None and pending.browser_id != browser_id:
            logger.warning('OAuth consent rejected: reason=other_browser client_id=%s', pending.client_id)
            return None
        return pending

    async def _pending_upstream(self, state: str, browser_id: str | None) -> PendingAuthorization:
        """The sign-in the upstream's callback under ``state`` finishes, which ``browser_id`` must have approved.

        Raises ``HTTPException`` when ``state`` names none of this server's sign-ins against the current upstream,
        including one recorded before sign-ins were tied to a browser, or when another browser delivers it.
        A sign-in recorded against another upstream is refused outright,
        since its code would otherwise be redeemed at a token endpoint that never issued it.
        """
        data = await self.store.get('mcp_auth_state', state)
        pending = self._parse_pending(data) if data and self._is_bound_here(data) else None
        if pending is None:
            logger.warning('OAuth callback rejected: reason=invalid_state')
            raise HTTPException(400, 'This sign-in has expired or was already finished')
        if browser_id is None or pending.browser_id != browser_id:
            logger.warning('OAuth callback rejected: reason=other_browser client_id=%s', pending.client_id)
            raise HTTPException(400, 'This sign-in was started in another browser')
        return pending

    @staticmethod
    def _parse_pending(data: dict[str, typing.Any]) -> PendingAuthorization | None:
        """``data`` as a sign-in, or ``None`` for a record from before sign-ins were tied to a browser."""
        try:
            return PendingAuthorization.model_validate(data)
        except pydantic.ValidationError:
            return None

    def _validate_upstream_response_issuer(self, pending: PendingAuthorization, iss: str | None) -> None:
        """Apply RFC 9207 to the upstream's authorization response, before its code is redeemed.

        A present ``iss`` must equal the issuer recorded for this request, compared exactly as the RFC requires.
        """
        recorded_issuer = pending.upstream_issuer
        if iss is None:
            return
        if not recorded_issuer:
            logger.warning(
                'Upstream authorization response carries iss=%s but auth.upstream.issuer is not set, '
                'so it cannot be checked. Set auth.upstream.issuer to enable RFC 9207 mix-up protection.',
                iss,
            )
            return
        if iss != recorded_issuer:
            logger.warning('OAuth callback rejected: reason=iss_mismatch iss=%s expected=%s', iss, recorded_issuer)
            raise HTTPException(400, 'Authorization response rejected: iss does not match the upstream issuer')

    async def _issue_mcp_token(
        self,
        client_id: str,
        scopes: list[str],
        upstream_scopes: list[str],
        api_access_token: str,
        api_refresh_token: str | None,
    ) -> OAuthToken:
        """Mint MCP access and refresh tokens and map them to the upstream API tokens, recording the upstream grant."""
        mcp_access = f'mcp_{secrets.token_hex(32)}'
        mcp_refresh = f'mcp_refresh_{secrets.token_hex(32)}'
        now = int(time.time())

        await self.store.set(
            'mcp_access_token',
            mcp_access,
            {
                'token': mcp_access,
                'client_id': client_id,
                'scopes': scopes,
                'upstream_scopes': upstream_scopes,
                'expires_at': now + self.issued_tokens.access_token_ttl,
                **self._binding(),
            },
            ttl=self.issued_tokens.access_token_ttl,
        )

        await self.store.set(
            'mcp_refresh_token',
            mcp_refresh,
            {
                'token': mcp_refresh,
                'client_id': client_id,
                'scopes': scopes,
                'upstream_scopes': upstream_scopes,
                'expires_at': now + self.issued_tokens.refresh_token_ttl,
                **self._binding(),
            },
            ttl=self.issued_tokens.refresh_token_ttl,
        )

        # mcp_access -> api_access drives tool calls.
        await self.store.set_mapping(
            'mcp_access_token',
            mcp_access,
            'api_access_token',
            api_access_token,
            ttl=self.issued_tokens.access_token_ttl,
        )
        # mcp_refresh -> api_access keeps the upstream token reachable through the refresh chain.
        await self.store.set_mapping(
            'mcp_refresh_token',
            mcp_refresh,
            'api_access_token',
            api_access_token,
            ttl=self.issued_tokens.refresh_token_ttl,
        )
        if api_refresh_token:
            await self.store.set_mapping(
                'mcp_refresh_token',
                mcp_refresh,
                'api_refresh_token',
                api_refresh_token,
                ttl=self.issued_tokens.refresh_token_ttl,
            )
        # Pair access and refresh in both directions for revoke lookup.
        await self.store.set_mapping(
            'mcp_access_token', mcp_access, 'mcp_refresh_token', mcp_refresh, ttl=self.issued_tokens.access_token_ttl
        )
        await self.store.set_mapping(
            'mcp_refresh_token', mcp_refresh, 'mcp_access_token', mcp_access, ttl=self.issued_tokens.refresh_token_ttl
        )

        # The granted scope is always stated, since RFC 6749 §5.1 requires it whenever it differs from the request.
        return OAuthToken(
            access_token=mcp_access,
            refresh_token=mcp_refresh,
            expires_in=self.issued_tokens.access_token_ttl,
            scope=' '.join(scopes) or None,
        )

    async def _store_api_token(self, client_id: str, token: str, expires_in: int) -> None:
        """Persist upstream access token metadata under ``api_access_token`` with TTL."""
        await self.store.set(
            'api_access_token',
            token,
            {
                'token': token,
                'client_id': client_id,
                'expires_at': int(time.time()) + expires_in,
            },
            ttl=expires_in,
        )

    async def _request_upstream_token(self, request_data: dict[str, typing.Any]) -> UpstreamTokenResponse:
        """POST ``request_data`` to ``upstream_token_url``.

        Audience parameters are added here rather than at each call site,
        so the initial exchange and every later refresh stay bound to the same API.
        Dropping them on refresh would return a token the upstream refuses,
        and that failure would only surface once the first token expired.

        Raises ``HTTPException`` when the upstream rejects the exchange.
        """
        # Grant fields last, so a stray audience key can never displace ``grant_type`` or the credentials.
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.upstream.token_url,
                data={**self.upstream.audience_params, **request_data},
                headers={'Accept': 'application/json'},
            )

            if response.status_code != 200:
                logger.warning(
                    'Upstream token exchange failed: status=%d url=%s',
                    response.status_code,
                    self.upstream.token_url,
                )
                raise HTTPException(400, f'Upstream token exchange failed: {response.text}')

            data = response.json()
            access_token = data.get('access_token')
            if not access_token:
                logger.warning('Upstream token exchange returned no access_token: url=%s', self.upstream.token_url)
                raise HTTPException(400, 'Upstream returned no access_token')

            logger.info(
                'Upstream token response: granted_scope=%r expires_in=%s', data.get('scope'), data.get('expires_in')
            )
            return UpstreamTokenResponse(
                access_token=access_token,
                refresh_token=data.get('refresh_token'),
                expires_in=data.get('expires_in', 3600),
                scope=data.get('scope'),
            )


class AuthorizationCodeFlowHandler(OAuthFlowHandler):
    """Build the per-user ``authorization_code`` setup: provider, ``AuthSettings``, and resolver."""

    def build(self, flow_context: OAuthFlowContext) -> OAuthFlowSetup:
        entry = flow_context.entry
        oauth_flow = flow_context.oauth_flow

        client_id = entry.auth.upstream.resolve_client_id()
        client_secret = entry.auth.upstream.resolve_client_secret()
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
        callback_url = f'{gateway_url}{flow_context.mount_path}{CALLBACK_PATH}'

        # The scopes every token carries and every request must hold, granted on top of a client's own scopes.
        # With auth.operation_scopes there are none, since each operation names its own scopes instead.
        mcp_endpoint_scopes = (
            [] if entry.auth.operation_scopes is not None else list(entry.auth.required_scopes) or [DEFAULT_MCP_SCOPE]
        )
        server_url = pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}')
        # The same string the metadata publishes as ``issuer``,
        # since RFC 9207 compares the two without any normalisation.
        issuer = str(server_url)
        clients = self._client_directory(flow_context, mcp_endpoint_scopes, issuer)

        provider = AuthorizationCodeProvider(
            store=flow_context.store,
            upstream=UpstreamOAuthClient(
                authorization_url=oauth_flow.authorization_url,
                token_url=oauth_flow.token_url,
                client_id=client_id,
                client_secret=client_secret,
                callback_url=callback_url,
                scopes=list(entry.auth.upstream.scopes),
                scope_descriptions=oauth_flow.scopes,
                audience_params=entry.auth.upstream.resolve_audience_params(),
                issuer=entry.auth.upstream.resolve_issuer(),
            ),
            issued_tokens=IssuedTokenPolicy(
                scopes=mcp_endpoint_scopes,
                access_token_ttl=entry.auth.mcp_access_token_ttl,
                refresh_token_ttl=entry.auth.mcp_refresh_token_ttl,
            ),
            issuer=issuer,
            prefix=entry.name,
            clients=clients,
        )
        settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=clients.registration_options(mcp_endpoint_scopes),
            required_scopes=mcp_endpoint_scopes,
        )

        logger.debug(
            'Authorization code flow set up for "%s": authorize=%s token=%s scopes=%s audience_params=%s',
            entry.name,
            oauth_flow.authorization_url,
            oauth_flow.token_url,
            entry.auth.upstream.scopes,
            entry.auth.upstream.resolve_audience_params(),
        )

        return OAuthFlowSetup(
            resolver=AuthorizationCodeAuthResolver(provider),
            provider=provider,
            settings=settings,
        )

    @staticmethod
    def _client_directory(
        flow_context: OAuthFlowContext, mcp_endpoint_scopes: list[str], issuer: str
    ) -> ClientDirectory:
        """The clients ``auth.clients`` declares, or Dynamic Client Registration when it declares none."""
        entry = flow_context.entry
        if entry.auth.clients:
            return PreRegisteredClientDirectory(entry.auth.clients, mcp_endpoint_scopes)
        logger.warning(
            'Server "%s" accepts Dynamic Client Registration, which the MCP 2026-07-28 spec deprecates. '
            'Declare the clients allowed to sign in under auth.clients instead.',
            entry.name,
        )
        return DynamicClientRegistry(flow_context.store, issuer)
