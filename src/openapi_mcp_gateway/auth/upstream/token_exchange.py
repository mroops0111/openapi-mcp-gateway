import asyncio
import collections.abc
import dataclasses
import logging
import time
import typing

import httpx

from .token_lifetime import DEFAULT_REFRESH_SKEW_SECONDS, DEFAULT_TOKEN_LIFETIME_SECONDS, parse_expires_in


logger = logging.getLogger(__name__)


# RFC 8693 §2.1 identifiers.
# Constants, not credentials, despite what the secret scanner reads them as.
TOKEN_EXCHANGE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:token-exchange'  # noqa: S105
ACCESS_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:access_token'  # noqa: S105

# Error codes by which an issuer refuses the scopes or target asked for, rather than the client or the request.
# RFC 6749 §5.2 defines invalid_scope and RFC 8693 §2.2.2 adds invalid_target.
REFUSAL_ERROR_CODES = frozenset({'invalid_scope', 'invalid_target'})


# A cached exchange is keyed by the subject token and the scopes asked for.
_CacheKey = tuple[str, tuple[str, ...]]


class UpstreamTokenExchangeError(RuntimeError):
    """A token exchange failed, for a reason that concerns the gateway's operator rather than the caller."""


class UpstreamTokenExchangeRefusedError(UpstreamTokenExchangeError):
    """The issuer refused to grant the scopes or target asked for, which is the caller's answer.

    The message names the scopes and the issuer's error code, and leaves out the rest of its response.
    """


@dataclasses.dataclass(frozen=True)
class UpstreamExchangedToken:
    """An upstream token an exchange returned, with the scopes the issuer granted on it.

    RFC 8693 §2.2.1 lets a response leave ``scope`` out when it equals what was asked for,
    so ``scopes`` are then the ones requested.
    """

    access_token: str
    scopes: tuple[str, ...]


class UpstreamTokenExchangeClient:
    """Exchange a caller's access token for one the upstream API accepts, per RFC 8693.

    Used by the ``token_exchange`` flow, where the gateway holds no credential for the person calling it.
    It is a client of the issuer's token endpoint rather than an ``UpstreamTokenSource``,
    since it needs the caller's token handed to it, which ``TokenExchangeBridge`` does.
    The MCP spec forbids relaying the caller's token to an upstream API,
    so the gateway asks the issuer for a second token naming that API instead.
    The exchanged token keeps the caller's ``sub``, which is what preserves per-user authorization across the hop.

    Results are cached per subject token and scopes asked for, since one MCP session makes many tool calls.
    The cache is keyed by the token itself rather than by user,
    so a re-authenticated caller never picks up credentials minted for an older session.
    A result is renewed ``refresh_skew_seconds`` before it expires, but never before half its lifetime,
    so a lifetime shorter than the skew still serves the calls made in a burst.
    Expired entries are dropped on each new exchange, so callers whose tokens rotate do not grow the cache forever.
    Concurrent first calls with one key share one exchange, while calls for other keys do not wait on it.

    ``transport`` routes the exchanges off-network, and ``clock`` is the monotonic time expiry is measured by,
    both for tests.

    Support is uneven across authorization servers.
    Keycloak has it generally available, authentik added it in 2026.8,
    Auth0 gates it behind a paid plan, and some servers only allow narrowing an existing audience.
    """

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        audience_params: dict[str, str] | None = None,
        refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: typing.Callable[[], float] = time.monotonic,
    ) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience_params = audience_params or {}
        self.refresh_skew_seconds = refresh_skew_seconds
        # Each cached result holds when it is renewed and when it expires, as monotonic times.
        self._cache: dict[_CacheKey, tuple[UpstreamExchangedToken, float, float]] = {}
        self._locks: dict[_CacheKey, asyncio.Lock] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._transport = transport
        self._clock = clock

    async def exchange(self, subject_token: str, scopes: collections.abc.Sequence[str]) -> UpstreamExchangedToken:
        """Return an upstream token for ``subject_token`` asking for ``scopes``, from cache when one is still fresh.

        No ``scope`` is sent when ``scopes`` is empty, so the issuer applies its own default.
        Raises ``UpstreamTokenExchangeRefusedError`` when the issuer refuses the scopes or target,
        ``UpstreamTokenExchangeError`` for any other failed exchange,
        and ``httpx.HTTPError`` when the issuer cannot be reached.
        """
        key = (subject_token, tuple(scopes))
        cached = self._cached(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                cached = self._cached(key)
                if cached is not None:
                    return cached
                self._drop_expired()
                return await self._request_exchange(key)
        finally:
            if not lock.locked():
                self._locks.pop(key, None)

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._cache.clear()
        self._locks.clear()

    def _drop_expired(self) -> None:
        """Forget every cached result whose token has expired."""
        now = self._clock()
        for key in [key for key, (_, _, expires_at) in self._cache.items() if now >= expires_at]:
            del self._cache[key]

    def _cached(self, key: _CacheKey) -> UpstreamExchangedToken | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        exchanged, renew_at, _ = entry
        if self._clock() >= renew_at:
            return None
        return exchanged

    async def _request_exchange(self, key: _CacheKey) -> UpstreamExchangedToken:
        subject_token, scopes = key
        request_data: dict[str, str] = {
            **self.audience_params,
            'grant_type': TOKEN_EXCHANGE_GRANT_TYPE,
            'subject_token': subject_token,
            'subject_token_type': ACCESS_TOKEN_TYPE,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        if scopes:
            request_data['scope'] = ' '.join(scopes)

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(transport=self._transport)

        logger.debug(
            'Token exchange: endpoint=%s audience_params=%s scopes=%s',
            self.token_endpoint,
            self.audience_params,
            scopes,
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
            error_code = _error_code_of(response)
            if response.status_code == 400 and error_code in REFUSAL_ERROR_CODES:
                raise UpstreamTokenExchangeRefusedError(
                    f"The authorization server refused to exchange the caller's token for the upstream scopes "
                    f'{" ".join(scopes) or "it defaults to"} ({error_code}).'
                )
            raise UpstreamTokenExchangeError(
                f'Token exchange failed ({response.status_code}): {response.text}. '
                'Check that the authorization server supports RFC 8693, '
                'that this client is allowed to exchange tokens, '
                'and that the configured audience names a target it knows.'
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamTokenExchangeError('Token exchange response is not JSON') from error
        access_token = payload.get('access_token') if isinstance(payload, dict) else None
        if not access_token:
            logger.warning('Token exchange response missing access_token: endpoint=%s', self.token_endpoint)
            raise UpstreamTokenExchangeError('Token exchange response missing access_token')

        granted = payload.get('scope')
        exchanged = UpstreamExchangedToken(
            access_token, tuple(str(granted).split()) if granted is not None else tuple(scopes)
        )
        expires_in = parse_expires_in(payload.get('expires_in')) or DEFAULT_TOKEN_LIFETIME_SECONDS
        now = self._clock()
        self._cache[key] = (
            exchanged,
            now + max(expires_in - self.refresh_skew_seconds, expires_in / 2),
            now + expires_in,
        )
        logger.info(
            'Token exchange succeeded: endpoint=%s expires_in=%s scope=%r',
            self.token_endpoint,
            expires_in,
            granted,
        )
        return exchanged


def _error_code_of(response: httpx.Response) -> str | None:
    """The OAuth ``error`` code of a failed token response, or ``None`` when its body states none."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get('error') if isinstance(payload, dict) else None
    return error if isinstance(error, str) else None
