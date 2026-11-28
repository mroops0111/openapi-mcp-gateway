import json

import httpx
import pytest
from mcp import Client
from mcp.types import RequestParamsMeta

from tests.constants import (
    SHOP_URL,
)
from tests.gateway import connect_in_memory, server_mcp
from tests.tool_result import text_of


class TestEndToEndToolInvocation:
    """Exercise the full assembly chain from spec to operations, tool registration, and the upstream HTTP call."""

    async def test_list_products_calls_upstream_with_query_params(self, gateway, mock_upstream):
        """Invoking the generated ``listProducts`` tool reaches the upstream URL with the right query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'tote'}])

        mock_upstream(handler)

        async with Client(server_mcp(gateway)) as client:
            result = await client.call_tool('list_products', {'limit': 5})

        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert captured['url'].startswith(f'{SHOP_URL}/products')

        assert result.is_error is False
        assert json.loads(text_of(result)) == [{'id': 1, 'name': 'tote'}]


class TestUpstreamHttpClientLifecycle:
    """Each server keeps one upstream client across calls and closes it when the gateway shuts down."""

    async def test_calls_reuse_one_client_closed_at_shutdown(
        self, gateway, upstream_requests, constructed_upstream_clients
    ):
        """Two calls go through one client, and the gateway's shutdown closes it."""

        async with gateway.lifespan(), Client(server_mcp(gateway), raise_exceptions=True) as client:
            await client.call_tool('list_products', {})
            await client.call_tool('list_products', {})

        assert len(upstream_requests) == 2
        assert len(constructed_upstream_clients) == 1
        with pytest.raises(RuntimeError, match='client has been closed'):
            await constructed_upstream_clients[0].request('GET', '/products')


class TestTraceContextPropagation:
    """W3C trace-context keys in a request's ``_meta`` are forwarded to the upstream HTTP call."""

    _TRACE: RequestParamsMeta = {
        'traceparent': '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
        'tracestate': 'rojo=00f067aa0ba902b7',
        'baggage': 'userId=alice',
    }

    async def test_trace_context_forwarded_to_upstream(self, gateway, mock_upstream):
        """traceparent / tracestate / baggage from the MCP call reach the upstream request verbatim."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = request.headers
            return httpx.Response(200, json=[])

        mock_upstream(handler)

        async with connect_in_memory(gateway, raise_exceptions=True) as client:
            await client.call_tool('list_products', {}, meta=self._TRACE)

        headers = captured['headers']
        assert headers['traceparent'] == self._TRACE['traceparent']
        assert headers['tracestate'] == self._TRACE['tracestate']
        assert headers['baggage'] == self._TRACE['baggage']

    async def test_no_trace_context_forwards_no_trace_headers(self, gateway, mock_upstream):
        """A call without trace context in ``_meta`` adds no trace headers upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = request.headers
            return httpx.Response(200, json=[])

        mock_upstream(handler)

        async with connect_in_memory(gateway, raise_exceptions=True) as client:
            await client.call_tool('list_products', {})

        headers = captured['headers']
        assert 'traceparent' not in headers
        assert 'tracestate' not in headers
        assert 'baggage' not in headers
