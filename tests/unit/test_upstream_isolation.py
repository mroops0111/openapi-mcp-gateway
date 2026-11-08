import asyncio
import collections
import logging
import re

import httpx
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from openapi_mcp_gateway.auth.resolver import AuthResolver
from openapi_mcp_gateway.client import APIClient
from openapi_mcp_gateway.exposure import ToolGenerator, UpstreamBinding
from openapi_mcp_gateway.openapi import OperationInfo, ParameterInfo
from tests.constants import API_URL


# Enough concurrent callers that their requests interleave on the shared client many times over.
_CALLERS = 40

# The caller in an httpx request log line, such as ``HTTP Request: GET https://api.example.com/me?caller=7 ...``.
_LOGGED_CALLER = re.compile(r'HTTP Request: GET \S+/me\?caller=(\d+)\b')


class _CallerAuthResolver(AuthResolver):
    """Authenticate each call as the caller its ``_meta`` names, as a per-user OAuth token would."""

    async def resolve(self, ctx: Context) -> dict[str, str]:
        meta = ctx.request_context.meta
        assert meta is not None
        return {'Authorization': f'Bearer token-{meta["caller"]}'}


def _trace_parent(caller: int) -> str:
    return f'00-{caller:032x}-{caller:016x}-01'


class TestConcurrentCallsShareNothingButConnections:
    """Calls through one server's shared upstream client never see each other's headers, cookies, or traces."""

    async def test_each_request_carries_only_its_own_caller(self, monkeypatch, caplog):
        """Interleaved calls each send their own auth, trace, and header argument, and no cookie any call was set.

        Each request is also logged once with its own URL, so the log lines of one caller never mix with another's.

        The caller is read off the query string, which travels apart from every header under test.
        A second round runs after the first has been answered, so a cookie the first round was set could leak into it.
        """
        constructed: list[APIClient] = []
        original_init = APIClient.__init__

        def counting_init(self, *args, **kwargs):
            constructed.append(self)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(APIClient, '__init__', counting_init)
        caplog.set_level(logging.INFO, logger='httpx')
        received: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            caller = request.url.params['caller']
            # Pauses that differ by caller let later requests overtake earlier ones before they are answered.
            await asyncio.sleep(int(caller) * 7 % 11 / 1000)
            received.append(request)
            return httpx.Response(200, json={}, headers={'set-cookie': f'session={caller}; Path=/'})

        mcp = MCPServer('test')
        binding = UpstreamBinding(
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
        ToolGenerator(mcp=mcp, binding=binding).register([operation])

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
        await binding.client.aclose()
