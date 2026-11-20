import asyncio
import collections
import logging
import re

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from openapi_mcp_gateway.auth.upstream.resolver import UpstreamAuthResolver
from openapi_mcp_gateway.exposure import ToolGenerator, UpstreamConnection
from openapi_mcp_gateway.openapi import OperationInfo, ParameterInfo
from openapi_mcp_gateway.upstream_http import UpstreamBinaryBody, UpstreamHttpClient, UpstreamSharedHttpClient
from tests.constants import API_URL


# Enough concurrent callers that their requests interleave on the shared client many times over.
_CALLERS = 40


# The caller in an httpx request log line, such as ``HTTP Request: GET https://api.example.com/me?caller=7 ...``.
_LOGGED_CALLER = re.compile(r'HTTP Request: GET \S+/me\?caller=(\d+)\b')


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
    """A binary media type comes back as a ``UpstreamBinaryBody`` holding the exact bytes, not decoded text."""
    body = b'\x89PNG\r\n\x1a\n\xff\xfe'

    def handler(_request):
        return httpx.Response(200, content=body, headers={'content-type': 'image/png'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1/photo')

    assert result == UpstreamBinaryBody(body, 'image/png', 'https://example.com/pet/1/photo')


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
        (b'\xff\xfe', UpstreamBinaryBody(b'\xff\xfe', 'application/octet-stream', 'https://example.com/blob')),
    ],
)
async def test_untyped_body_is_text_only_when_it_decodes(body, expected):
    """A body without a ``Content-Type`` is text when it decodes as UTF-8, and an octet stream otherwise."""

    def handler(_request):
        return httpx.Response(200, content=body)

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/blob')

    assert result == expected


@pytest.mark.parametrize('status_code', [404, 500])
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

    async with UpstreamHttpClient(base_url='https://example.com', transport=httpx.MockTransport(handler)) as client:
        await client.request('GET', '/me')
        await client.request('GET', '/me')

    assert sent_cookies == [None, None]


async def test_a_cookie_header_on_the_request_is_sent_as_given():
    """A forwarded ``Cookie`` header reaches the upstream unchanged, with nothing an earlier response set."""
    sent_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_cookies.append(request.headers.get('cookie'))
        return httpx.Response(200, json={}, headers={'set-cookie': 'session=alice; Path=/'})

    async with UpstreamHttpClient(base_url='https://example.com', transport=httpx.MockTransport(handler)) as client:
        await client.request('GET', '/me')
        await client.request('GET', '/me', headers={'Cookie': 'session=bob'})

    assert sent_cookies == [None, 'session=bob']


class TestUpstreamSharedHttpClient:
    """One ``UpstreamHttpClient`` per upstream server, bound to the event loop that first used it."""

    async def test_the_same_client_serves_every_call_on_a_loop(self):
        """Calls on one loop reuse one client, so its connections are kept alive between them."""
        shared = UpstreamSharedHttpClient('https://example.com')

        assert shared.get() is shared.get()
        await shared.aclose()

    def test_another_loop_gets_a_fresh_client(self):
        """A client is not carried onto a second loop, since its connections belong to the first."""
        shared = UpstreamSharedHttpClient('https://example.com')

        async def current() -> UpstreamHttpClient:
            return shared.get()

        first, second = asyncio.run(current()), asyncio.run(current())

        assert first is not second

    async def test_aclose_closes_the_client_and_the_next_call_opens_another(self):
        """Closing on the client's own loop closes it, and a later call does not reuse the closed one."""
        shared = UpstreamSharedHttpClient('https://example.com')
        client = shared.get()

        await shared.aclose()

        assert client._client.is_closed
        assert shared.get() is not client
        await shared.aclose()

    def test_aclose_on_another_loop_forgets_the_client_without_touching_it(self):
        """A shutdown hook run on a fresh loop, as stdio does, leaves the old loop's client alone."""
        shared = UpstreamSharedHttpClient('https://example.com')

        async def open_client() -> UpstreamHttpClient:
            return shared.get()

        client = asyncio.run(open_client())
        asyncio.run(shared.aclose())

        assert not client._client.is_closed
        assert shared._client is None


class TestConcurrentCallsShareNothingButConnections:
    """Calls through one server's shared upstream client never see each other's headers, cookies, or traces."""

    async def test_each_request_carries_only_its_own_caller(self, monkeypatch, caplog):
        """Interleaved calls each send their own auth, trace, and header argument, and no cookie any call was set.

        Each request is also logged once with its own URL, so the log lines of one caller never mix with another's.

        The caller is read off the query string, which travels apart from every header under test.
        A second round runs after the first has been answered, so a cookie the first round was set could leak into it.
        """
        constructed: list[UpstreamHttpClient] = []
        original_init = UpstreamHttpClient.__init__

        def counting_init(self, *args, **kwargs):
            constructed.append(self)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(UpstreamHttpClient, '__init__', counting_init)
        caplog.set_level(logging.INFO, logger='httpx')
        received: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            caller = request.url.params['caller']
            # Pauses that differ by caller let later requests overtake earlier ones before they are answered.
            await asyncio.sleep(int(caller) * 7 % 11 / 1000)
            received.append(request)
            return httpx.Response(200, json={}, headers={'set-cookie': f'session={caller}; Path=/'})

        mcp = MCPServer('test')
        connection = UpstreamConnection(
            base_url=API_URL,
            auth_resolver=_CallerAuthResolver(),
            transport=httpx.MockTransport(handler),
            headers={'X-Static': 'server-wide'},
        )
        operation = OperationInfo(
            operation_id='get_me',
            method='get',
            path='/me',
            parameters=[
                ParameterInfo(name='caller', location='query', required=True, schema={'type': 'string'}),
                ParameterInfo(name='X-Request-Id', location='header', required=True, schema={'type': 'string'}),
            ],
        )
        ToolGenerator(mcp=mcp, connection=connection).register([operation])

        async with Client(mcp, raise_exceptions=True) as client:
            for _round in range(2):
                calls = [
                    client.call_tool(
                        'get_me',
                        {'caller': str(caller), 'X_Request_Id': f'request-{caller}'},
                        meta={'caller': caller, 'traceparent': _trace_parent(caller)},
                    )
                    for caller in range(_CALLERS)
                ]
                for result in await asyncio.gather(*calls):
                    assert not result.is_error

        assert len(received) == 2 * _CALLERS
        for request in received:
            caller = int(request.url.params['caller'])
            assert request.headers['authorization'] == f'Bearer token-{caller}'
            assert request.headers['traceparent'] == _trace_parent(caller)
            assert request.headers['x-request-id'] == f'request-{caller}'
            assert request.headers['x-static'] == 'server-wide'
            assert 'cookie' not in request.headers
        logged_callers = collections.Counter(
            int(match.group(1)) for record in caplog.records if (match := _LOGGED_CALLER.search(record.getMessage()))
        )
        assert logged_callers == dict.fromkeys(range(_CALLERS), 2)
        assert len(constructed) == 1
        await connection.client.aclose()


def _client_with_handler(handler):
    """Build an ``UpstreamHttpClient`` whose transport is a ``MockTransport`` over ``handler``."""
    client = UpstreamHttpClient(base_url='https://example.com')
    client._client = httpx.AsyncClient(
        base_url='https://example.com',
        transport=httpx.MockTransport(handler),
    )
    return client


class _CallerAuthResolver(UpstreamAuthResolver):
    """Authenticate each call as the caller its ``_meta`` names, as a per-user OAuth token would."""

    async def resolve(self, ctx: Context) -> dict[str, str]:
        meta = ctx.request_context.meta
        assert meta is not None
        return {'Authorization': f'Bearer token-{meta["caller"]}'}


def _trace_parent(caller: int) -> str:
    return f'00-{caller:032x}-{caller:016x}-01'
