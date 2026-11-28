import contextlib
import typing

from mcp import Client
from mcp.server import MCPServer

from openapi_mcp_gateway.auth.mcp.client_metadata import McpClientMetadataResolver
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig


def build_gateway(
    gateway_url: str,
    servers: list[dict[str, typing.Any]],
    client_metadata_resolver: McpClientMetadataResolver | None = None,
) -> Gateway:
    """A gateway published at ``gateway_url`` serving ``servers``, each a server config as YAML would give it."""
    return Gateway.from_config(
        GatewayConfig.model_validate({'url': gateway_url, 'servers': servers}),
        client_metadata_resolver=client_metadata_resolver,
    )


def server_mcp(gateway: Gateway, name: str | None = None) -> MCPServer:
    """The MCP entry point of ``gateway``'s server called ``name``, or of its only server when ``name`` is unset."""
    servers = gateway.describe_servers()
    if name is None:
        assert len(servers) == 1, f'the gateway serves {len(servers)} servers, so name the one to reach'
        return servers[0].mcp
    return next(server.mcp for server in servers if server.name == name)


@contextlib.asynccontextmanager
async def connect_in_memory(
    gateway: Gateway, name: str | None = None, **options: typing.Any
) -> typing.AsyncIterator[Client]:
    """An MCP SDK client of ``gateway``'s server, connected in memory, built with ``options``."""
    async with Client(server_mcp(gateway, name), **options) as client:
        yield client


async def tool_names(gateway: Gateway, name: str | None = None) -> list[str]:
    """The tools ``gateway``'s server lists to an MCP client, in registration order."""
    async with connect_in_memory(gateway, name) as client:
        return [tool.name for tool in (await client.list_tools()).tools]
