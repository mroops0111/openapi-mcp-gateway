import abc
import typing

from mcp.server.fastmcp import Context

from .token_source import TokenSource


class AuthResolver(abc.ABC):
    """Strategy: produce the upstream HTTP headers for one MCP tool call.

    The contract is intentionally header-shaped, not value-shaped: each
    implementation returns the full set of headers it wants to set on the
    upstream request (``Authorization``, ``X-API-Key``, …). ``ToolGenerator``
    merges that dict into the outgoing request without further interpretation.
    """

    @abc.abstractmethod
    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Return headers to attach to the upstream request, or ``{}`` for none."""


class NullAuthResolver(AuthResolver):
    """Resolver that contributes no headers (public upstream APIs)."""

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Always return an empty dict."""
        return {}


class StaticAuthResolver(AuthResolver):
    """Fixed credential header configured when the gateway starts.

    Used for ``bearer`` (``Authorization: Bearer …``) and ``api_key``
    (``<api_key_header>: …``) auth types — the upstream header name is
    captured at construction time so different schemes route to the right
    field.
    """

    def __init__(self, header_value: str, header_name: str = 'Authorization') -> None:
        """Bind the literal header name + value to attach on each request."""
        self._header_name = header_name
        self._header_value = header_value

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Return the single configured header."""
        return {self._header_name: self._header_value}


class AuthorizationCodeAuthResolver(AuthResolver):
    """Exchange MCP bearer tokens for upstream API bearer tokens via the authorization_code flow."""

    def __init__(self, provider: typing.Any) -> None:
        """Keep a reference to ``AuthorizationCodeProvider`` (``Any`` avoids import cycles)."""
        # provider is AuthorizationCodeProvider — use Any to avoid circular import
        self._provider = provider

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Look up the upstream token via the active MCP access token and emit ``Authorization``."""
        api_token = await self._provider.get_api_access_token()
        if api_token:
            return {'Authorization': f'Bearer {api_token}'}
        return {}


class TokenSourceAuthResolver(AuthResolver):
    """Resolver that delegates to a ``TokenSource`` for dynamic bearer tokens.

    Used by service-level OAuth flows (e.g., ``client_credentials``) where the
    same token is shared across all MCP clients and is fetched/refreshed by
    the gateway itself.
    """

    def __init__(self, token_source: TokenSource) -> None:
        """Bind a ``TokenSource`` whose tokens are sent as ``Authorization: Bearer …``."""
        self._token_source = token_source

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Fetch the current token and emit it as an ``Authorization`` header."""
        token = await self._token_source.get_token()
        if token:
            return {'Authorization': f'Bearer {token}'}
        return {}


class PassthroughAuthResolver(AuthResolver):
    """Forward selected headers from the MCP client's incoming request to the upstream call.

    Used when the gateway and the upstream API live in the same OAuth realm —
    typically the FastAPI in-process integration. ``header_names`` lists the
    headers to copy verbatim (case-insensitive); the default forwards only
    ``Authorization`` so the resolver stays usable on its own.
    """

    def __init__(self, header_names: tuple[str, ...] = ('Authorization',)) -> None:
        """Bind the header names this resolver should forward."""
        self._header_names = header_names

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Return any of ``header_names`` present on the live MCP request."""
        request_context = ctx.request_context
        if request_context is None:
            return {}
        request = getattr(request_context, 'request', None)
        if request is None:
            return {}
        headers = getattr(request, 'headers', None)
        if headers is None:
            return {}
        result: dict[str, str] = {}
        for name in self._header_names:
            value = headers.get(name) or headers.get(name.lower())
            if value is not None:
                result[name] = value
        return result


class CompositeAuthResolver(AuthResolver):
    """Compose multiple resolvers; later resolvers override earlier ones on key collision.

    Used to combine an incoming-header forwarder (``PassthroughAuthResolver``
    for ``X-API-Key`` / ``Cookie`` etc.) with a credential-minting resolver
    (e.g. ``TokenSourceAuthResolver`` for ``client_credentials``). Place the
    resolver that should win for the ``Authorization`` header last.
    """

    def __init__(self, resolvers: typing.Sequence[AuthResolver]) -> None:
        """Snapshot ``resolvers`` in chain order."""
        self._resolvers = list(resolvers)

    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Merge each resolver's headers in order; later entries overwrite earlier ones."""
        merged: dict[str, str] = {}
        for resolver in self._resolvers:
            merged.update(await resolver.resolve(ctx))
        return merged
