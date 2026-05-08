import abc
import typing

from mcp.server.fastmcp import Context

from .token_source import TokenSource


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


class AuthorizationCodeAuthResolver(AuthResolver):
    """Exchange MCP bearer tokens for upstream API bearer tokens via the authorization_code flow."""

    def __init__(self, provider: typing.Any) -> None:
        """Keep a reference to ``AuthorizationCodeProvider`` (``Any`` avoids import cycles)."""
        # provider is AuthorizationCodeProvider — use Any to avoid circular import
        self._provider = provider

    async def resolve(self, ctx: Context) -> str | None:
        """Lookup upstream token using the current MCP access token context."""
        api_token = await self._provider.get_api_access_token()
        if api_token:
            return f'Bearer {api_token}'
        return None


class TokenSourceAuthResolver(AuthResolver):
    """Resolver that delegates to a ``TokenSource`` for dynamic bearer tokens.

    Used by service-level OAuth flows (e.g., ``client_credentials``) where the
    same token is shared across all MCP clients and is fetched/refreshed by
    the gateway itself.
    """

    def __init__(self, token_source: TokenSource) -> None:
        """Bind a ``TokenSource`` whose tokens are sent as ``Authorization: Bearer …``."""
        self._token_source = token_source

    async def resolve(self, ctx: Context) -> str | None:
        """Fetch the current token and format it as an HTTP ``Authorization`` header."""
        token = await self._token_source.get_token()
        if token:
            return f'Bearer {token}'
        return None
