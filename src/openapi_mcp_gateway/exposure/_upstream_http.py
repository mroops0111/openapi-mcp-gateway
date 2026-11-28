import asyncio
import collections.abc
import contextvars
import dataclasses
import http.cookiejar
import json
import logging
import typing
import urllib.parse

import httpx


logger = logging.getLogger(__name__)

# The media type a binary body without a ``Content-Type`` is reported as.
_FALLBACK_BINARY_MEDIA_TYPE = 'application/octet-stream'

# What stands in for a credential's value wherever a URL carrying it is written.
REDACTED = '[REDACTED]'

# The query parameters the request being sent carries credentials in, read by the httpx request log's filter.
_REDACTED_QUERY: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar('redacted_query', default=frozenset())

# The connection pool of one upstream client.
# No cap on open connections, so concurrent calls never queue for one, as when each call opened its own.
# Idle ones are kept for reuse up to httpx's default.
_POOL_LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=20)


@dataclasses.dataclass(frozen=True)
class UpstreamBinaryBody:
    """A non-text upstream body, kept as bytes so a file, image or PDF reaches the client intact."""

    content: bytes
    media_type: str
    url: str


class UpstreamHttpClient:
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
        query_string: str = '',
        data: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
        redacted_query: collections.abc.Collection[str] = (),
    ) -> dict[str, typing.Any] | UpstreamBinaryBody:
        """Send ``method`` to ``path`` with the already encoded ``query_string``, and return the decoded body.

        Returns decoded JSON for ``application/json`` and ``+json`` responses, ``{'status': code}`` for empty 204 responses,
        ``{'data': text}`` for any other body that is text, or a ``UpstreamBinaryBody`` for one that is not.
        Raises ``httpx.HTTPStatusError`` on HTTP error status codes.
        The ``redacted_query`` parameters carry credentials, so every URL written from here hides their values.
        """
        request_kwargs: dict[str, typing.Any] = {}
        if data is not None and method.upper() in ('POST', 'PUT', 'PATCH'):
            request_kwargs['json'] = data
        if headers:
            request_kwargs['headers'] = headers

        logger.debug('Upstream request: %s %s', method.upper(), path)
        # A URL built with its query leaves the query's encoding as it is, so the comma a serializer kept stays one.
        request_url = httpx.URL(path, query=query_string.encode()) if query_string else path
        redacted = _REDACTED_QUERY.set(frozenset(redacted_query))
        try:
            response = await self._client.request(method.upper(), request_url, **request_kwargs)
        finally:
            _REDACTED_QUERY.reset(redacted)
        url = redact_query(response.request.url, redacted_query)
        if response.is_error:
            logger.warning(
                'Upstream error: %s %s → %d %s', method.upper(), url, response.status_code, response.reason_phrase
            )
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase} for '{url}': {response.text}",
                request=response.request,
                response=response,
            )
        logger.debug('Upstream response: %s %s → %d', method.upper(), path, response.status_code)

        if response.status_code == 204 or not response.content:
            return {'status': response.status_code}

        content_type = response.headers.get('content-type', '')
        if is_json_media_type(content_type):
            try:
                return response.json()
            except json.JSONDecodeError:
                return {'data': response.text}
        return self._decode_non_json_body(response, url)

    @staticmethod
    def _decode_non_json_body(response: httpx.Response, url: str) -> dict[str, typing.Any] | UpstreamBinaryBody:
        """Return a non-JSON body as ``{'data': text}`` when it is text, and as a ``UpstreamBinaryBody`` otherwise.

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
            return UpstreamBinaryBody(response.content, media_type or _FALLBACK_BINARY_MEDIA_TYPE, url)


class UpstreamSharedHttpClient:
    """One ``UpstreamHttpClient`` for one upstream server, reused by every call to it so connections are kept alive.

    It holds no per-call state.
    Callers send every header on the request, and ``UpstreamHttpClient`` stores no cookie.
    A client belongs to the event loop that first used it, since its connections do,
    so a call on another loop gets a fresh client.
    """

    def __init__(self, base_url: str, timeout: float = 90, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport
        self._client: UpstreamHttpClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> UpstreamHttpClient:
        """Return the client for the running event loop, opening it on first use."""
        loop = _running_loop()
        if self._client is None or self._loop is not loop:
            # A client left on a finished loop cannot be closed from this one, so it is dropped with its loop.
            self._client = UpstreamHttpClient(base_url=self._base_url, timeout=self._timeout, transport=self._transport)
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        """Close the client if it belongs to the running loop, and forget it either way."""
        client, loop = self._client, self._loop
        self._client = self._loop = None
        if client is not None and loop is _running_loop():
            await client.aclose()


def redact_query(url: httpx.URL, names: collections.abc.Collection[str]) -> str:
    """``url`` as text, with the value of every query parameter in ``names`` replaced by ``REDACTED``."""
    if not names or not any(name in names for name in url.params):
        return str(url)
    query = '&'.join(
        f'{urllib.parse.quote(name)}={REDACTED}' if name in names else urllib.parse.urlencode([(name, value)])
        for name, value in url.params.multi_items()
    )
    return str(url.copy_with(query=query.encode()))


def is_json_media_type(content_type: str) -> bool:
    """Whether ``content_type`` is JSON, ``application/json`` or a type with the ``+json`` suffix (RFC 6839 §3.1)."""
    media_type = content_type.partition(';')[0].strip().lower()
    return media_type == 'application/json' or media_type.endswith('+json')


class _RedactQueryInRequestLog(logging.Filter):
    """Hide the credentials in the URL of httpx's ``HTTP Request`` line, which it writes at INFO for every request."""

    def filter(self, record: logging.LogRecord) -> bool:
        names = _REDACTED_QUERY.get()
        arguments = record.args
        if names and isinstance(arguments, tuple) and len(arguments) > 1 and isinstance(arguments[1], httpx.URL):
            record.args = (arguments[0], redact_query(arguments[1], names), *arguments[2:])
        return True


class _RefuseCookiesPolicy(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: http.cookiejar.Cookie, request: typing.Any) -> bool:
        return False


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# httpx writes every request's full URL at INFO, so its logger hides the credentials as the gateway's own lines do.
logging.getLogger('httpx').addFilter(_RedactQueryInRequestLog())
