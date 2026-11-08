import asyncio
import dataclasses
import http.cookiejar
import json
import logging
import typing

import httpx


logger = logging.getLogger(__name__)

# The media type a binary body without a ``Content-Type`` is reported as.
_FALLBACK_BINARY_MEDIA_TYPE = 'application/octet-stream'

# The connection pool of one upstream client.
# No cap on open connections, so concurrent calls never queue for one, as when each call opened its own.
# Idle ones are kept for reuse up to httpx's default.
_POOL_LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=20)


@dataclasses.dataclass(frozen=True)
class BinaryBody:
    """A non-text upstream body, kept as bytes so a file, image or PDF reaches the client intact."""

    content: bytes
    media_type: str
    url: str


class APIClient:
    """Thin async HTTP wrapper around ``httpx.AsyncClient`` for upstream calls.

    A cookie the upstream sets is never stored, so nothing one call receives is sent on another,
    which keeps a client shared across callers free of any one caller's session.
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Configure base URL, default headers, timeout, and optional async transport.

        ``transport`` lets callers route requests off-network,
        e.g. ``httpx.ASGITransport`` for in-process FastAPI or ``httpx.MockTransport`` in tests.
        """
        client_kwargs: dict[str, typing.Any] = {
            'base_url': base_url,
            'timeout': timeout,
            'headers': headers or {},
            'cookies': http.cookiejar.CookieJar(policy=_RefuseCookiesPolicy()),
            'limits': _POOL_LIMITS,
        }
        if transport is not None:
            client_kwargs['transport'] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        await self.aclose()

    def set_auth_header(self, token: str, scheme: str = 'Bearer') -> None:
        self._client.headers['Authorization'] = f'{scheme} {token}'

    def set_header(self, key: str, value: str) -> None:
        self._client.headers[key] = value

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, typing.Any] | None = None,
        data: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, typing.Any] | BinaryBody:
        """Send ``method`` to ``path`` and return the decoded body.

        Returns decoded JSON for ``application/json`` responses, ``{'status': code}`` for empty 204 responses,
        ``{'data': text}`` for any other body that is text, or a ``BinaryBody`` for one that is not.
        Raises ``httpx.HTTPStatusError`` on HTTP error status codes.
        """
        request_kwargs: dict[str, typing.Any] = {}
        if params:
            request_kwargs['params'] = params
        if data is not None and method.upper() in ('POST', 'PUT', 'PATCH'):
            request_kwargs['json'] = data
        if headers:
            request_kwargs['headers'] = headers

        logger.debug('Upstream request: %s %s', method.upper(), path)
        response = await self._client.request(method.upper(), path, **request_kwargs)
        if response.is_error:
            logger.warning(
                'Upstream error: %s %s → %d %s',
                method.upper(),
                response.request.url,
                response.status_code,
                response.reason_phrase,
            )
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase} for '{response.request.url}': {response.text}",
                request=response.request,
                response=response,
            )
        logger.debug('Upstream response: %s %s → %d', method.upper(), path, response.status_code)

        if response.status_code == 204 or not response.content:
            return {'status': response.status_code}

        content_type = response.headers.get('content-type', '')
        if 'application/json' in content_type:
            try:
                return response.json()
            except json.JSONDecodeError:
                return {'data': response.text}
        return self._decode_non_json_body(response)

    @staticmethod
    def _decode_non_json_body(response: httpx.Response) -> dict[str, typing.Any] | BinaryBody:
        """Return a non-JSON body as ``{'data': text}`` when it is text, and as a ``BinaryBody`` otherwise.

        The body decides, not a list of media types.
        A declared ``charset`` makes it text, since only text has one.
        Otherwise it is text when it decodes as UTF-8, which real binary such as an image or an archive fails early.
        """
        if response.charset_encoding is not None:
            return {'data': response.text}
        try:
            return {'data': response.content.decode('utf-8')}
        except UnicodeDecodeError:
            media_type = response.headers.get('content-type', '').partition(';')[0].strip().lower()
            return BinaryBody(response.content, media_type or _FALLBACK_BINARY_MEDIA_TYPE, str(response.request.url))


class SharedAPIClient:
    """One ``APIClient`` for one upstream server, reused by every call to it so connections are kept alive.

    It holds no per-call state: callers send every header on the request, and ``APIClient`` stores no cookie.
    A client belongs to the event loop that first used it, since its connections do,
    so a call on another loop gets a fresh client.
    """

    def __init__(self, base_url: str, timeout: float = 90, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport
        self._client: APIClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> APIClient:
        """Return the client for the running event loop, opening it on first use."""
        loop = _running_loop()
        if self._client is None or self._loop is not loop:
            # A client left on a finished loop cannot be closed from this one, so it is dropped with its loop.
            self._client = APIClient(base_url=self._base_url, timeout=self._timeout, transport=self._transport)
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        """Close the client if it belongs to the running loop, and forget it either way."""
        client, loop = self._client, self._loop
        self._client = self._loop = None
        if client is not None and loop is _running_loop():
            await client.aclose()


class _RefuseCookiesPolicy(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: http.cookiejar.Cookie, request: typing.Any) -> bool:
        return False


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
