import contextlib
import pathlib
import sys
import typing

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client

from openapi_mcp_gateway.gateway import Gateway
from tests.serving import serve


class TestTransports:
    """Every transport the CLI accepts starts and answers MCP, rather than starting far enough to log and then dying."""

    @pytest.mark.parametrize('transport', ['streamable-http', 'sse', 'stdio'])
    async def test_answers_initialize_and_lists_tools(self, gateway, shop_json_path, transport):
        """A client of each transport completes ``initialize`` and lists the shop tools."""
        with anyio.fail_after(10):
            async with _connect(gateway, shop_json_path, transport) as mcp_client:
                server_info = mcp_client.server_info
                tools = await mcp_client.list_tools()

        assert server_info is not None
        assert server_info.name.startswith('shop')
        assert tools.tools


@contextlib.asynccontextmanager
async def _connect(gateway: Gateway, spec_path: pathlib.Path, transport: str) -> typing.AsyncIterator[Client]:
    """Connect an MCP client over ``transport``, through the CLI for stdio and ``gateway``'s app for HTTP."""
    if transport == 'stdio':
        cli_args = ['serve', '--spec', str(spec_path), '--name', 'shop', '--transport', 'stdio']
        async with Client(
            StdioServerParameters(command=sys.executable, args=['-m', 'openapi_mcp_gateway.cli', *cli_args])
        ) as mcp_client:
            yield mcp_client
        return
    async with serve(gateway.build_app(transport)) as base_url:
        target = f'{base_url}/shop/mcp' if transport == 'streamable-http' else sse_client(f'{base_url}/shop/sse')
        async with Client(target) as mcp_client:
            yield mcp_client
