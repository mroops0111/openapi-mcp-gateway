import contextlib
import json
import logging
import typing

from mcp import Client
from mcp.types import CallToolResult, TextContent

from ..gateway import Gateway
from ..openapi import load_spec
from ..settings import GatewayConfig, ServerConfig


logger = logging.getLogger(__name__)


class ToolCallError(RuntimeError):
    """A tool the benchmark called returned an error result."""


def build_gateway(server: ServerConfig) -> Gateway:
    """Build a gateway serving ``server`` alone, exactly as ``--config`` would."""
    return Gateway.from_config(GatewayConfig(servers=[server]))


@contextlib.asynccontextmanager
async def connect(server: ServerConfig) -> typing.AsyncIterator[Client]:
    """Yield an MCP client connected in-process to a gateway serving ``server``.

    The client speaks MCP to the same server object the gateway mounts,
    so a benchmark sees what a model would see, without a port or a network hop.
    """
    gateway = build_gateway(server)
    async with Client(gateway.describe_servers()[0].mcp) as client:
        yield client


async def call_tool(client: Client, name: str, arguments: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Call tool ``name`` and return its structured result, raising ``ToolCallError`` on an error result."""
    result: CallToolResult = await client.call_tool(name, arguments)
    if result.is_error:
        raise ToolCallError(f'{name}({arguments}) failed: {_text(result)}')
    if result.structured_content is not None:
        return dict(result.structured_content)
    return json.loads(_text(result))


def operation_summaries(server: ServerConfig) -> dict[str, str]:
    """Each operation's name, as the model is shown it, mapped to its spec summary, or ``''`` when it has none.

    Names come from the gateway itself, so renames and policy apply, and summaries from the spec.
    """
    static = server.model_copy(update={'exposure': server.exposure.model_copy(update={'style': 'static'})})
    gateway = build_gateway(static)
    raw = load_spec(server.spec)
    summaries: dict[str, str] = {}
    for tool in gateway.describe()['servers'][0]['tools']:
        operation = raw.get('paths', {}).get(tool['path'], {}).get(tool['method'], {})
        summaries[tool['name']] = ' '.join(str(operation.get('summary') or '').split())
    return summaries


def _text(result: CallToolResult) -> str:
    return ''.join(block.text for block in result.content if isinstance(block, TextContent))
