"""AuthResolver — Strategy pattern for resolving upstream API auth headers."""

import abc
import typing

from mcp.server.fastmcp import Context


class AuthResolver(abc.ABC):
    """Abstract base class for resolving the upstream auth header for a tool call."""

    @abc.abstractmethod
    async def resolve(self, ctx: Context) -> str | None:
        """Resolve the upstream auth header value (e.g. 'Bearer xxx').

        Returns None if no auth is available or required.
        """


class NullAuthResolver(AuthResolver):
    """No authentication — for public APIs."""

    async def resolve(self, ctx: Context) -> str | None:
        return None


class StaticAuthResolver(AuthResolver):
    """Static auth header — for bearer tokens and API keys configured at startup."""

    def __init__(self, header_value: str) -> None:
        self._header_value = header_value

    async def resolve(self, ctx: Context) -> str | None:
        return self._header_value


class OAuthAuthResolver(AuthResolver):
    """Resolves upstream token by looking up the MCP access token in the store."""

    def __init__(self, provider: typing.Any) -> None:
        # provider is GatewayOAuthProvider — use Any to avoid circular import
        self._provider = provider

    async def resolve(self, ctx: Context) -> str | None:
        api_token = await self._provider.get_api_access_token()
        if api_token:
            return f'Bearer {api_token}'
        return None
