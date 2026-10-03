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
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.exceptions import HTTPException

from ...stores.base import TokenStore
from ..resolver import AuthorizationCodeAuthResolver
from .base import OAuthFlowContext, OAuthFlowHandler, OAuthFlowSetup


logger = logging.getLogger(__name__)


# The scope the gateway issues and demands when it is the authorization server.
# It has no upstream meaning, since the gateway mints these tokens itself,
# and exists only so the OAuth machinery has a scope to name.
DEFAULT_MCP_SCOPE = 'api'


@dataclasses.dataclass(frozen=True)
class UpstreamOAuthClient:
    """What the gateway needs to act as an OAuth client at the upstream authorization server.

    ``callback_url`` is the gateway's own address, and belongs here because it is only ever
    meaningful as the ``redirect_uri`` of this conversation.

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
    """

    def __init__(
        self,
        store: TokenStore,
        upstream: UpstreamOAuthClient,
        issued_tokens: IssuedTokenPolicy,
        issuer: str,
        prefix: str = 'gateway',
    ) -> None:
        self.store = store
        self.upstream = upstream
        self.issued_tokens = issued_tokens
        self.issuer = issuer
        self._prefix = prefix

    # MCP SDK OAuthAuthorizationServerProvider interface

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        # A registration stored before issuer binding lives under the bare client_id and is never read here.
        # The client is then unknown to this server and registers again, which is the migration path.
        data = await self.store.get('mcp_client', self._client_key(client_id))
        if data:
            return OAuthClientInformationFull(**data)
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError('client_id is required')
        await self.store.set(
            'mcp_client',
            self._client_key(client_info.client_id),
            client_info.model_dump(exclude_none=True, mode='json'),
        )
        logger.info(
            'Registered MCP client: client_id=%s application_type=%s prefix=%s',
            client_info.client_id,
            client_info.application_type,
            self._prefix,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Return the upstream authorize URL after stashing PKCE/state payload in ``store``."""
        client_id = self._require_client_id(client)
        state = params.state or secrets.token_hex(16)

        # The expected issuer is recorded with the request it belongs to, as the MCP spec requires,
        # so the callback checks the response against the server this request was sent to,
        # even if configuration changed in between.
        state_data = {
            'redirect_uri': str(params.redirect_uri),
            'code_challenge': params.code_challenge,
            'redirect_uri_provided_explicitly': params.redirect_uri_provided_explicitly,
            'client_id': client_id,
            'upstream_issuer': self.upstream.issuer,
            'upstream_authorization_server': self.upstream.authorization_server,
        }
        await self.store.set('mcp_auth_state', state, state_data, ttl=900)

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
            api_access_token=api_access_token,
            api_refresh_token=api_refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = await self.store.get('mcp_access_token', token)
        if data and self._is_bound_here(data):
            return AccessToken(**data)
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

        if not await self.store.get('api_access_token', api_access_token):
            if not api_refresh_token:
                logger.warning('OAuth refresh rejected: reason=upstream_expired_no_refresh client_id=%s', client_id)
                raise TokenError(
                    error='invalid_grant',
                    error_description='Upstream token expired and no refresh token available; re-authenticate',
                )
            api_access_token, new_refresh, expires_in = await self._request_upstream_token(
                {
                    'client_id': self.upstream.client_id,
                    'client_secret': self.upstream.client_secret,
                    'refresh_token': api_refresh_token,
                    'grant_type': 'refresh_token',
                }
            )
            api_refresh_token = new_refresh or api_refresh_token
            await self._store_api_token(client_id, api_access_token, expires_in)

        new_token = await self._issue_mcp_token(
            client_id=client_id,
            scopes=scopes or data.get('scopes', []),
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

    async def handle_upstream_callback(self, code: str, state: str, iss: str | None = None) -> str:
        """Finish the browser redirect by swapping the upstream ``code`` for MCP auth artefacts.

        Validates ``state`` and the RFC 9207 ``iss``, exchanges tokens at the upstream token endpoint,
        persists the upstream credentials, builds an MCP authorization code, and returns the client redirect URI.
        The ``iss`` check runs before the code leaves the gateway,
        since a mix-up attack succeeds the moment the code reaches the wrong token endpoint.
        """
        state_data = await self.store.get('mcp_auth_state', state)
        if not state_data:
            logger.warning('OAuth callback rejected: reason=invalid_state state=%s', state)
            raise HTTPException(400, 'Invalid state parameter')

        self._validate_upstream_response_issuer(state_data, iss)

        redirect_uri = state_data['redirect_uri']
        code_challenge = state_data['code_challenge']
        redirect_uri_provided_explicitly = state_data['redirect_uri_provided_explicitly']
        client_id = state_data['client_id']

        api_access_token, api_refresh_token, expires_in = await self._request_upstream_token(
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
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'redirect_uri_provided_explicitly': redirect_uri_provided_explicitly,
                'expires_at': time.time() + 300,
                'scopes': list(self.issued_tokens.scopes),
                'code_challenge': code_challenge,
                **self._binding(),
            },
            ttl=300,
        )

        await self._store_api_token(client_id, api_access_token, expires_in)

        await self.store.set_mapping('mcp_auth_code', mcp_auth_code, 'api_access_token', api_access_token, ttl=300)
        if api_refresh_token:
            await self.store.set_mapping(
                'mcp_auth_code', mcp_auth_code, 'api_refresh_token', api_refresh_token, ttl=300
            )

        await self.store.delete('mcp_auth_state', state)

        logger.info(
            'Upstream OAuth callback handled: client_id=%s expires_in=%s refresh=%s',
            client_id,
            expires_in,
            bool(api_refresh_token),
        )
        # RFC 9207: name this authorization server in its own response, as its metadata advertises,
        # so an MCP client talking to several authorization servers can tell which one answered.
        return construct_redirect_uri(redirect_uri, code=mcp_auth_code, state=state, iss=self.issuer)

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

    def _client_key(self, client_id: str) -> str:
        """Key a registration by this server's issuer as well as its ``client_id``.

        The MCP spec keys client credentials by issuer.
        Keying the record, rather than only checking a field in it,
        also keeps two servers from overwriting each other's registration of the same ``client_id``.
        """
        return f'{self.issuer} {client_id}'

    def _binding(self) -> dict[str, str]:
        """The fields that tie a persisted code or token to this issuer and this upstream."""
        return {'issuer': self.issuer, 'upstream_authorization_server': self.upstream.authorization_server}

    def _is_bound_here(self, data: dict[str, typing.Any]) -> bool:
        """Whether a stored code or token was minted by this server against the current upstream.

        A record from before binding existed carries neither field and so never matches.
        Accepting it would leave the cross-server hole open for as long as such records live.
        """
        return all(data.get(field) == value for field, value in self._binding().items())

    def _validate_upstream_response_issuer(self, state_data: dict[str, typing.Any], iss: str | None) -> None:
        """Apply RFC 9207 to the upstream's authorization response, before its code is redeemed.

        A present ``iss`` must equal the issuer recorded for this request, compared exactly as the RFC requires.
        A response to a request recorded against another upstream is refused outright,
        since its code would otherwise be redeemed at a token endpoint that never issued it.
        """
        if state_data.get('upstream_authorization_server') != self.upstream.authorization_server:
            logger.warning('OAuth callback rejected: reason=upstream_changed')
            raise HTTPException(400, 'The upstream authorization server changed during sign-in; start again')

        recorded_issuer = state_data.get('upstream_issuer')
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
        api_access_token: str,
        api_refresh_token: str | None,
    ) -> OAuthToken:
        """Mint MCP access and refresh tokens and map them to the upstream API tokens."""
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

        return OAuthToken(
            access_token=mcp_access,
            refresh_token=mcp_refresh,
            expires_in=self.issued_tokens.access_token_ttl,
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

    async def _request_upstream_token(self, request_data: dict[str, typing.Any]) -> tuple[str, str | None, int]:
        """POST ``request_data`` to ``upstream_token_url``.

        Audience parameters are added here rather than at each call site,
        so the initial exchange and every later refresh stay bound to the same API.
        Dropping them on refresh would return a token the upstream refuses,
        and that failure would only surface once the first token expired.

        Returns ``(access_token, refresh_token | None, expires_in)``,
        raising ``HTTPException`` when the upstream rejects the exchange.
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
            return access_token, data.get('refresh_token'), data.get('expires_in', 3600)


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
        callback_url = f'{gateway_url}{flow_context.mount_path}/auth/callback'

        # auth.required_scopes names what a caller must hold. The gateway is the issuer here,
        # so it is also what the gateway advertises and grants, rather than something it merely checks.
        mcp_scopes = list(entry.auth.required_scopes) or [DEFAULT_MCP_SCOPE]
        server_url = pydantic.AnyHttpUrl(f'{gateway_url}{flow_context.mount_path}')

        provider = AuthorizationCodeProvider(
            store=flow_context.store,
            upstream=UpstreamOAuthClient(
                authorization_url=oauth_flow.authorization_url,
                token_url=oauth_flow.token_url,
                client_id=client_id,
                client_secret=client_secret,
                callback_url=callback_url,
                scopes=list(entry.auth.upstream.scopes),
                audience_params=entry.auth.upstream.resolve_audience_params(),
                issuer=entry.auth.upstream.resolve_issuer(),
            ),
            issued_tokens=IssuedTokenPolicy(
                scopes=mcp_scopes,
                access_token_ttl=entry.auth.mcp_access_token_ttl,
                refresh_token_ttl=entry.auth.mcp_refresh_token_ttl,
            ),
            # The same string the metadata publishes as ``issuer``,
            # since RFC 9207 compares the two without any normalisation.
            issuer=str(server_url),
            prefix=entry.name,
        )
        settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=mcp_scopes,
                default_scopes=mcp_scopes,
            ),
            required_scopes=mcp_scopes,
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
