import dataclasses
import typing

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, Tool

from tests.context import stub_context


@dataclasses.dataclass(frozen=True)
class ServedTool:
    """A tool an ``MCPServer`` serves, reached only through the server's public API, as a client would see it."""

    mcp: MCPServer
    name: str

    async def listed(self) -> Tool:
        """The tool as ``tools/list`` shows it."""
        return next(tool for tool in await self.mcp.list_tools() if tool.name == self.name)

    async def input_schema(self) -> dict[str, typing.Any]:
        """The input schema the tool advertises."""
        return (await self.listed()).input_schema

    async def call(
        self,
        arguments: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> CallToolResult:
        """Call the tool with ``arguments``, in a request that carries ``headers``.

        The stub context declares no client capabilities, so no call can ask the client for input.
        """
        result = await self.mcp.call_tool(self.name, arguments or {}, stub_context(headers))
        assert isinstance(result, CallToolResult)
        return result


async def tool_names(mcp: MCPServer) -> list[str]:
    """The names of the tools ``mcp`` lists, in registration order."""
    return [tool.name for tool in await mcp.list_tools()]
