import asyncio

import httpx
import pytest

from openapi_mcp_gateway.client import APIClient, BinaryBody, SharedAPIClient


def _client_with_handler(handler):
    """Build an ``APIClient`` whose transport is a ``MockTransport`` over ``handler``."""
    client = APIClient(base_url='https://example.com')
    client._client = httpx.AsyncClient(
        base_url='https://example.com',
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_json_response_parsed():
    """``application/json`` responses are returned as the decoded dict."""

    def handler(_request):
        return httpx.Response(200, json={'id': 1, 'name': 'fido'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1')

    assert result == {'id': 1, 'name': 'fido'}


async def test_204_no_content():
    """``204 No Content`` returns a synthetic ``{'status': 204}`` instead of parsing."""

    def handler(_request):
        return httpx.Response(204)

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'status': 204}


async def test_non_json_body_wrapped():
    """Non-JSON content types are wrapped as ``{'data': text}``."""

    def handler(_request):
        return httpx.Response(200, text='hello', headers={'content-type': 'text/plain'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/text')

    assert result == {'data': 'hello'}


async def test_invalid_json_with_json_content_type_falls_back_to_text():
    """Bodies that claim ``application/json`` but fail to parse fall back to ``{'data': text}``."""

    def handler(_request):
        return httpx.Response(200, content=b'Pet deleted', headers={'content-type': 'application/json'})

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'data': 'Pet deleted'}


async def test_binary_body_kept_as_bytes():
    """A binary media type comes back as a ``BinaryBody`` holding the exact bytes, not decoded text."""
    body = b'\x89PNG\r\n\x1a\n\xff\xfe'

    def handler(_request):
        return httpx.Response(200, content=body, headers={'content-type': 'image/png'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1/photo')

    assert result == BinaryBody(body, 'image/png', 'https://example.com/pet/1/photo')


@pytest.mark.parametrize(
    'content_type',
    ['application/xml', 'application/problem+json', 'application/toml', 'text/csv', 'application/octet-stream'],
)
async def test_body_that_decodes_as_utf8_stays_text(content_type):
    """The body decides, so text under any media type keeps the ``{'data': text}`` shape."""

    def handler(_request):
        return httpx.Response(200, content=b'<pet/>', headers={'content-type': content_type})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1')

    assert result == {'data': '<pet/>'}


async def test_declared_charset_decodes_as_text():
    """A ``charset`` makes the body text, decoded with that charset rather than as UTF-8."""

    def handler(_request):
        return httpx.Response(
            200, content='café'.encode('latin-1'), headers={'content-type': 'text/plain; charset=latin-1'}
        )

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/menu')

    assert result == {'data': 'café'}


@pytest.mark.parametrize(
    ('body', 'expected'),
    [
        (b'plain words', {'data': 'plain words'}),
        (b'\xff\xfe', BinaryBody(b'\xff\xfe', 'application/octet-stream', 'https://example.com/blob')),
    ],
)
async def test_untyped_body_is_text_only_when_it_decodes(body, expected):
    """A body without a ``Content-Type`` is text when it decodes as UTF-8, and an octet stream otherwise."""

    def handler(_request):
        return httpx.Response(200, content=body)

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/blob')

    assert result == expected


@pytest.mark.parametrize('status_code', [400, 401, 404, 500, 503])
async def test_http_error_raises_status_error(status_code):
    """4xx and 5xx responses raise ``httpx.HTTPStatusError`` carrying the response."""

    def handler(_request):
        return httpx.Response(status_code, json={'error': 'boom'})

    async with _client_with_handler(handler) as client:
        with pytest.raises(httpx.HTTPStatusError) as exception_info:
            await client.request('GET', '/pet/missing')

    assert exception_info.value.response.status_code == status_code


async def test_cookies_the_upstream_sets_are_never_sent_back():
    """A ``Set-Cookie`` from one response is not stored, so a later request through the same client carries none."""
    sent_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_cookies.append(request.headers.get('cookie'))
        return httpx.Response(200, json={}, headers={'set-cookie': 'session=alice; Path=/'})

    async with APIClient(base_url='https://example.com', transport=httpx.MockTransport(handler)) as client:
        await client.request('GET', '/me')
        await client.request('GET', '/me')

    assert sent_cookies == [None, None]


async def test_a_cookie_header_on_the_request_is_sent_as_given():
    """A forwarded ``Cookie`` header reaches the upstream unchanged, with nothing an earlier response set."""
    sent_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_cookies.append(request.headers.get('cookie'))
        return httpx.Response(200, json={}, headers={'set-cookie': 'session=alice; Path=/'})

    async with APIClient(base_url='https://example.com', transport=httpx.MockTransport(handler)) as client:
        await client.request('GET', '/me')
        await client.request('GET', '/me', headers={'Cookie': 'session=bob'})

    assert sent_cookies == [None, 'session=bob']


class TestSharedAPIClient:
    """One ``APIClient`` per upstream server, bound to the event loop that first used it."""

    async def test_the_same_client_serves_every_call_on_a_loop(self):
        """Calls on one loop reuse one client, so its connections are kept alive between them."""
        shared = SharedAPIClient('https://example.com')

        assert shared.get() is shared.get()
        await shared.aclose()

    def test_another_loop_gets_a_fresh_client(self):
        """A client is not carried onto a second loop, since its connections belong to the first."""
        shared = SharedAPIClient('https://example.com')

        async def current() -> APIClient:
            return shared.get()

        first, second = asyncio.run(current()), asyncio.run(current())

        assert first is not second

    async def test_aclose_closes_the_client_and_the_next_call_opens_another(self):
        """Closing on the client's own loop closes it, and a later call does not reuse the closed one."""
        shared = SharedAPIClient('https://example.com')
        client = shared.get()

        await shared.aclose()

        assert client._client.is_closed
        assert shared.get() is not client
        await shared.aclose()

    def test_aclose_on_another_loop_forgets_the_client_without_touching_it(self):
        """A shutdown hook run on a fresh loop, as stdio does, leaves the old loop's client alone."""
        shared = SharedAPIClient('https://example.com')

        async def open_client() -> APIClient:
            return shared.get()

        client = asyncio.run(open_client())
        asyncio.run(shared.aclose())

        assert not client._client.is_closed
        assert shared._client is None
