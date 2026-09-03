import abc
import asyncio
import logging
import time
import typing

import httpx


logger = logging.getLogger(__name__)


DEFAULT_REFRESH_SKEW_SECONDS = 30.0
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600.0


class TokenSource(abc.ABC):
    """Abstract bearer-token provider used by per-request auth resolvers.

    Implementations cache the access token and refresh it transparently before expiry.
    ``get_token`` should be cheap; concrete implementations must de-dup concurrent refreshes.
    """

    @abc.abstractmethod
    async def get_token(self) -> str:
        """Return a currently-valid bearer token, fetching or refreshing as needed."""

    async def aclose(self) -> None:
        """Release resources held by the token source. Default is a no-op."""
        return None


class ClientCredentialsTokenSource(TokenSource):
    """Fetch and cache an OAuth2 ``client_credentials`` token from an upstream IdP.

    The token is fetched lazily on first ``get_token``,
    and cached until ``refresh_skew_seconds`` before its declared expiry.
    Concurrent refreshes are de-duped through an internal ``asyncio.Lock``.

    ``audience_params`` names the API the token is for,
    for an upstream whose API and authorization server are different parties.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
        audience_params: dict[str, str] | None = None,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or []
        self.refresh_skew_seconds = refresh_skew_seconds
        self.audience_params = audience_params or {}
        self._access_token: str | None = None
        self._expires_at_monotonic: float = 0.0
        self._lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None

    async def get_token(self) -> str:
        """Return a cached token if still valid, otherwise fetch a new one."""
        if self._is_token_valid():
            return typing.cast(str, self._access_token)
        async with self._lock:
            if self._is_token_valid():
                return typing.cast(str, self._access_token)
            await self._fetch_token()
            return typing.cast(str, self._access_token)

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _is_token_valid(self) -> bool:
        return (
            self._access_token is not None and time.monotonic() < self._expires_at_monotonic - self.refresh_skew_seconds
        )

    async def _fetch_token(self) -> None:
        # Grant fields last, so a stray audience key can never displace ``grant_type`` or the credentials.
        request_data: dict[str, str] = {
            **self.audience_params,
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        if self.scopes:
            request_data['scope'] = ' '.join(self.scopes)

        if self._http_client is None:
            self._http_client = httpx.AsyncClient()

        logger.debug(
            'Client credentials token fetch: url=%s scopes=%s audience_params=%s',
            self.token_url,
            self.scopes,
            self.audience_params,
        )
        response = await self._http_client.post(
            self.token_url,
            data=request_data,
            headers={'Accept': 'application/json'},
        )

        if response.status_code != 200:
            logger.warning(
                'Client credentials token fetch failed: status=%d url=%s',
                response.status_code,
                self.token_url,
            )
            raise RuntimeError(f'Client credentials token fetch failed ({response.status_code}): {response.text}')

        payload = response.json()
        access_token = payload.get('access_token')
        if not access_token:
            logger.warning('Client credentials token response missing access_token: url=%s', self.token_url)
            raise RuntimeError('Client credentials token response missing access_token')

        expires_in = float(payload.get('expires_in', DEFAULT_TOKEN_LIFETIME_SECONDS))
        self._access_token = access_token
        self._expires_at_monotonic = time.monotonic() + expires_in
        logger.info(
            'Client credentials token acquired: url=%s expires_in=%s scope=%r',
            self.token_url,
            expires_in,
            payload.get('scope'),
        )


# RFC 8693 §2.1 identifiers. Constants, not credentials, despite what the secret scanner reads them as.
TOKEN_EXCHANGE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:token-exchange'  # noqa: S105
ACCESS_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:access_token'  # noqa: S105


class TokenExchangeTokenSource:
    """Exchange a caller's access token for one the upstream API accepts, per RFC 8693.

    Used by the ``token_exchange`` flow, where the gateway holds no credential for the person calling it.
    The MCP spec forbids relaying the caller's token to an upstream API,
    so the gateway asks the issuer for a second token naming that API instead.
    The exchanged token keeps the caller's ``sub``, which is what preserves per-user authorization across the hop.

    Results are cached per subject token, since one MCP session makes many tool calls.
    The cache is keyed by the token itself rather than by user,
    so a re-authenticated caller never picks up credentials minted for an older session.

    Support is uneven across authorization servers.
    Keycloak has it generally available, authentik added it in 2026.8,
    Auth0 gates it behind a paid plan, and some servers only allow narrowing an existing audience.
    """

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        audience_params: dict[str, str],
        scopes: list[str] | None = None,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
    ) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience_params = audience_params
        self.scopes = scopes or []
        self.refresh_skew_seconds = refresh_skew_seconds
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None

    async def exchange(self, subject_token: str) -> str:
        """Return an upstream token for ``subject_token``, from cache when one is still fresh."""
        cached = self._cached(subject_token)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached(subject_token)
            if cached is not None:
                return cached
            return await self._request_exchange(subject_token)

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._cache.clear()

    def _cached(self, subject_token: str) -> str | None:
        entry = self._cache.get(subject_token)
        if entry is None:
            return None
        token, expires_at = entry
        if time.monotonic() >= expires_at - self.refresh_skew_seconds:
            return None
        return token

    async def _request_exchange(self, subject_token: str) -> str:
        request_data: dict[str, str] = {
            **self.audience_params,
            'grant_type': TOKEN_EXCHANGE_GRANT_TYPE,
            'subject_token': subject_token,
            'subject_token_type': ACCESS_TOKEN_TYPE,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        if self.scopes:
            request_data['scope'] = ' '.join(self.scopes)

        if self._http_client is None:
            self._http_client = httpx.AsyncClient()

        logger.debug(
            'Token exchange: endpoint=%s audience_params=%s scopes=%s',
            self.token_endpoint,
            self.audience_params,
            self.scopes,
        )
        response = await self._http_client.post(
            self.token_endpoint,
            data=request_data,
            headers={'Accept': 'application/json'},
        )

        if response.status_code != 200:
            logger.warning(
                'Token exchange failed: status=%d endpoint=%s',
                response.status_code,
                self.token_endpoint,
            )
            raise RuntimeError(
                f'Token exchange failed ({response.status_code}): {response.text}. '
                'Check that the authorization server supports RFC 8693, '
                'that this client is allowed to exchange tokens, '
                'and that the configured audience names a target it knows.'
            )

        payload = response.json()
        access_token = payload.get('access_token')
        if not access_token:
            logger.warning('Token exchange response missing access_token: endpoint=%s', self.token_endpoint)
            raise RuntimeError('Token exchange response missing access_token')

        expires_in = float(payload.get('expires_in', DEFAULT_TOKEN_LIFETIME_SECONDS))
        self._cache[subject_token] = (access_token, time.monotonic() + expires_in)
        logger.info(
            'Token exchange succeeded: endpoint=%s expires_in=%s scope=%r',
            self.token_endpoint,
            expires_in,
            payload.get('scope'),
        )
        return access_token
