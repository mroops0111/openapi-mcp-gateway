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

    The token is fetched lazily on first ``get_token`` and cached until
    ``refresh_skew_seconds`` before its declared expiry.
    Concurrent refreshes are de-duped through an internal ``asyncio.Lock``.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str] | None = None,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or []
        self.refresh_skew_seconds = refresh_skew_seconds
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
        request_data: dict[str, str] = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        if self.scopes:
            request_data['scope'] = ' '.join(self.scopes)

        if self._http_client is None:
            self._http_client = httpx.AsyncClient()

        logger.debug('Client credentials token fetch: url=%s scopes=%s', self.token_url, self.scopes)
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
