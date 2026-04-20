"""Generic OAuth2 authorization server provider for the MCP gateway.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol,
acting as an intermediary between MCP clients and upstream OAuth providers.
"""

import secrets
import time
import typing
import urllib.parse

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.exceptions import HTTPException

from ..stores.base import TokenStore


MCP_ACCESS_TOKEN_TTL = 3600  # 1 hour
MCP_REFRESH_TOKEN_TTL = 86400  # 24 hours
MCP_SCOPES = ['api']


class GatewayOAuthProvider:
    """Generic OAuth provider that bridges MCP OAuth ↔ upstream OAuth.

    For each server that uses OAuth2, one instance is created.
    It handles:
      - MCP client registration & token lifecycle
      - Redirecting users to the upstream authorization URL
      - Exchanging upstream auth codes for upstream tokens
      - Mapping MCP tokens ↔ upstream tokens in the store
    """

    def __init__(
        self,
        store: TokenStore,
        upstream_auth_url: str,
        upstream_token_url: str,
        client_id: str,
        client_secret: str,
        callback_url: str,
        scopes: list[str] | None = None,
        prefix: str = 'gateway',
    ) -> None:
        self.store = store
        self.upstream_auth_url = upstream_auth_url
        self.upstream_token_url = upstream_token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.scopes = scopes or []
        self._prefix = prefix

    # ── MCP SDK OAuthAuthorizationServerProvider interface ──

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = await self.store.get('mcp_client', client_id)
        if data:
            return OAuthClientInformationFull(**data)
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError('client_id is required')
        await self.store.set(
            'mcp_client',
            client_info.client_id,
            client_info.model_dump(exclude_none=True, mode='json'),
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        client_id = self._require_client_id(client)
        state = params.state or secrets.token_hex(16)

        state_data = {
            'redirect_uri': str(params.redirect_uri),
            'code_challenge': params.code_challenge,
            'redirect_uri_provided_explicitly': params.redirect_uri_provided_explicitly,
            'client_id': client_id,
        }
        await self.store.set('mcp_auth_state', state, state_data, ttl=900)

        query_params = {
            'client_id': self.client_id,
            'redirect_uri': self.callback_url,
            'state': state,
            'response_type': 'code',
        }
        if self.scopes:
            query_params['scope'] = ' '.join(self.scopes)

        return f'{self.upstream_auth_url}?{urllib.parse.urlencode(query_params)}'

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_auth_code', authorization_code)
        if data and data['client_id'] == client_id:
            return AuthorizationCode(**data)
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_auth_code', authorization_code.code)

        if not data or data['client_id'] != client_id:
            raise TokenError(error='invalid_grant', error_description='Invalid authorization code')

        api_access_token = await self.store.get_mapping(
            'mcp_auth_code', authorization_code.code, 'api_access_token'
        ) or await self.store.get_mapping('client', client_id, 'api_access_token')

        if not api_access_token:
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
        if data:
            return AccessToken(**data)
        return None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_refresh_token', refresh_token)
        if data and data['client_id'] == client_id:
            return RefreshToken(**data)
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        client_id = self._require_client_id(client)
        data = await self.store.get('mcp_refresh_token', refresh_token.token)

        if not data or data['client_id'] != client_id:
            raise TokenError(error='invalid_grant', error_description='Invalid refresh token')

        api_access_token = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'api_access_token')
        api_refresh_token = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'api_refresh_token')

        if not api_access_token:
            raise TokenError(error='invalid_grant', error_description='Upstream token mapping lost')

        # Check if upstream access token is still valid
        if not await self.store.get('api_access_token', api_access_token):
            if not api_refresh_token:
                raise TokenError(
                    error='invalid_grant',
                    error_description='Upstream token expired and no refresh token available; re-authenticate',
                )
            api_access_token, new_refresh, expires_in = await self._request_upstream_token(
                {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
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

        # Revoke old tokens
        old_access = await self.store.get_mapping('mcp_refresh_token', refresh_token.token, 'mcp_access_token')
        if old_access:
            await self.store.delete('mcp_access_token', old_access)
        await self.store.delete('mcp_refresh_token', refresh_token.token)

        return new_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
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

    # ── Gateway-specific methods ──

    async def handle_upstream_callback(self, code: str, state: str) -> str:
        """Handle the upstream OAuth callback.

        Exchanges the upstream code for tokens, stores them,
        creates an MCP auth code, and returns the redirect URI.
        """
        state_data = await self.store.get('mcp_auth_state', state)
        if not state_data:
            raise HTTPException(400, 'Invalid state parameter')

        redirect_uri = state_data['redirect_uri']
        code_challenge = state_data['code_challenge']
        redirect_uri_provided_explicitly = state_data['redirect_uri_provided_explicitly']
        client_id = state_data['client_id']

        # Exchange upstream code for tokens
        api_access_token, api_refresh_token, expires_in = await self._request_upstream_token(
            {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'code': code,
                'redirect_uri': self.callback_url,
                'grant_type': 'authorization_code',
            }
        )

        # Create MCP auth code
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
                'scopes': MCP_SCOPES,
                'code_challenge': code_challenge,
            },
            ttl=300,
        )

        # Store upstream tokens
        await self._store_api_token(client_id, api_access_token, expires_in)

        # Create mappings
        await self.store.set_mapping('mcp_auth_code', mcp_auth_code, 'api_access_token', api_access_token, ttl=300)
        if api_refresh_token:
            await self.store.set_mapping(
                'mcp_auth_code', mcp_auth_code, 'api_refresh_token', api_refresh_token, ttl=300
            )
        await self.store.set_mapping('client', client_id, 'api_access_token', api_access_token)

        # Clean up state
        await self.store.delete('mcp_auth_state', state)

        return construct_redirect_uri(redirect_uri, code=mcp_auth_code, state=state)

    async def get_api_access_token(self) -> str | None:
        """Get the upstream API access token for the current request.

        Uses the MCP access token from the request context to look up
        the corresponding upstream token.
        """
        mcp_access_token = get_access_token()
        if not mcp_access_token:
            return None
        return await self.store.get_mapping('mcp_access_token', mcp_access_token.token, 'api_access_token')

    # ── Private helpers ──

    @staticmethod
    def _require_client_id(client: OAuthClientInformationFull) -> str:
        if not client.client_id:
            raise ValueError('client_id is required')
        return client.client_id

    async def _issue_mcp_token(
        self,
        client_id: str,
        scopes: list[str],
        api_access_token: str,
        api_refresh_token: str | None,
    ) -> OAuthToken:
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
                'expires_at': now + MCP_ACCESS_TOKEN_TTL,
            },
            ttl=MCP_ACCESS_TOKEN_TTL,
        )

        await self.store.set(
            'mcp_refresh_token',
            mcp_refresh,
            {
                'token': mcp_refresh,
                'client_id': client_id,
                'scopes': scopes,
                'expires_at': now + MCP_REFRESH_TOKEN_TTL,
            },
            ttl=MCP_REFRESH_TOKEN_TTL,
        )

        # mcp_access → api_access (for tool calls)
        await self.store.set_mapping(
            'mcp_access_token', mcp_access, 'api_access_token', api_access_token, ttl=MCP_ACCESS_TOKEN_TTL
        )
        # mcp_refresh → api_access (for refresh chain)
        await self.store.set_mapping(
            'mcp_refresh_token', mcp_refresh, 'api_access_token', api_access_token, ttl=MCP_REFRESH_TOKEN_TTL
        )
        if api_refresh_token:
            await self.store.set_mapping(
                'mcp_refresh_token', mcp_refresh, 'api_refresh_token', api_refresh_token, ttl=MCP_REFRESH_TOKEN_TTL
            )
        # Pair access ↔ refresh for revoke lookup
        await self.store.set_mapping(
            'mcp_access_token', mcp_access, 'mcp_refresh_token', mcp_refresh, ttl=MCP_ACCESS_TOKEN_TTL
        )
        await self.store.set_mapping(
            'mcp_refresh_token', mcp_refresh, 'mcp_access_token', mcp_access, ttl=MCP_REFRESH_TOKEN_TTL
        )

        return OAuthToken(
            access_token=mcp_access,
            refresh_token=mcp_refresh,
            expires_in=MCP_ACCESS_TOKEN_TTL,
        )

    async def _store_api_token(self, client_id: str, token: str, expires_in: int) -> None:
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
        """Exchange credentials with the upstream OAuth token endpoint.

        Returns: (access_token, refresh_token | None, expires_in)
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.upstream_token_url,
                data=request_data,
                headers={'Accept': 'application/json'},
            )

            if response.status_code != 200:
                raise HTTPException(400, f'Upstream token exchange failed: {response.text}')

            data = response.json()
            access_token = data.get('access_token')
            if not access_token:
                raise HTTPException(400, 'Upstream returned no access_token')

            return access_token, data.get('refresh_token'), data.get('expires_in', 3600)
