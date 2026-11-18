import abc

from mcp.server import MCPServer


class AuthRoutes(abc.ABC):
    """Browser-facing routes one server's auth needs beside the MCP SDK's own, such as a consent page."""

    @abc.abstractmethod
    def register(self, mcp: MCPServer) -> None:
        """Mount the routes on ``mcp``, under the server's own path."""
