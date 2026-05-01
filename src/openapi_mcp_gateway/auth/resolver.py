import abc
import typing

from mcp.server.fastmcp import Context


class AuthResolver(abc.ABC):
    """Protocol for building the ``Authorization`` header for upstream HTTP calls."""

    @abc.abstractmethod
    async def resolve(self, ctx: Context) -> str | None:
        """Return the header value to send upstream, or ``None`` if unauthenticated.

        Implementations typically return a ``Bearer`` string or ``None``.
        """


class NullAuthResolver(AuthResolver):
    """Resolver that always omits authentication (public upstream APIs)."""

    async def resolve(self, ctx: Context) -> str | None:
        """Always return ``None``."""
        return None


class StaticAuthResolver(AuthResolver):
    """Fixed ``Authorization`` (or raw token) configured when the gateway starts."""

    def __init__(self, header_value: str) -> None:
        """Store the literal header value to attach on each request."""
        self._header_value = header_value

    async def resolve(self, ctx: Context) -> str | None:
        """Return the configured header string."""
        return self._header_value


class OAuthAuthResolver(AuthResolver):
    """Exchange MCP bearer tokens for upstream API bearer tokens via OAuth."""

    def __init__(self, provider: typing.Any) -> None:
        """Keep a reference to ``GatewayOAuthProvider`` (``Any`` avoids import cycles)."""
        # provider is GatewayOAuthProvider — use Any to avoid circular import
        self._provider = provider

    async def resolve(self, ctx: Context) -> str | None:
        """Lookup upstream token using the current MCP access token context."""
        api_token = await self._provider.get_api_access_token()
        if api_token:
            return f'Bearer {api_token}'
        return None
