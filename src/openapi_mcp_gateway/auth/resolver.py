import abc
import typing

from mcp.server.fastmcp import Context

from .token_source import TokenSource


class AuthResolver(abc.ABC):
    """Strategy that produces the upstream HTTP headers for one MCP tool call.

    The contract is header-shaped, not value-shaped:
    implementations return the full set of headers (``Authorization``, ``X-API-Key``, ...) they want on the request,
    and ``ToolGenerator`` merges that dict in unchanged.
    """

    @abc.abstractmethod
    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Return headers to attach to the upstream request, or ``{}`` for none."""


class NullAuthResolver(AuthResolver):
    """Resolver that contributes no headers (used for public upstream APIs)."""

    async def resolve(self, ctx: Context) -> dict[str, str]:
        return {}


class StaticAuthResolver(AuthResolver):
    """Fixed credential header captured at construction time.

    Used for ``bearer`` (``Authorization: Bearer ...``) and ``api_key`` (``<api_key_header>: ...``) configs.
    """

    def __init__(self, header_value: str, header_name: str = 'Authorization') -> None:
        self._header_name = header_name
        self._header_value = header_value

    async def resolve(self, ctx: Context) -> dict[str, str]:
        return {self._header_name: self._header_value}


class AuthorizationCodeAuthResolver(AuthResolver):
    """Resolve the upstream bearer for the per-user ``authorization_code`` flow."""

    def __init__(self, provider: typing.Any) -> None:
        # provider is AuthorizationCodeProvider; Any is used to avoid a circular import.
        self._provider = provider

    async def resolve(self, ctx: Context) -> dict[str, str]:
        api_token = await self._provider.get_api_access_token()
        if api_token:
            return {'Authorization': f'Bearer {api_token}'}
        return {}


class TokenSourceAuthResolver(AuthResolver):
    """Resolver that delegates to a ``TokenSource`` for dynamic bearer tokens.

    Used by service-level OAuth flows (e.g. ``client_credentials``) where one token is shared across all MCP clients,
    and the gateway handles refreshes.
    """

    def __init__(self, token_source: TokenSource) -> None:
        self._token_source = token_source

    async def resolve(self, ctx: Context) -> dict[str, str]:
        token = await self._token_source.get_token()
        if token:
            return {'Authorization': f'Bearer {token}'}
        return {}


class PassthroughAuthResolver(AuthResolver):
    """Forward selected headers from the live MCP request to the upstream call.

    Used when the gateway and the upstream API share an OAuth realm, typically the FastAPI in-process integration.
    ``header_names`` lists which headers to copy verbatim (case-insensitive);
    the default forwards only ``Authorization``.
    """

    def __init__(self, header_names: tuple[str, ...] = ('Authorization',)) -> None:
        self._header_names = header_names

    async def resolve(self, ctx: Context) -> dict[str, str]:
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

    Used to combine an incoming-header forwarder (e.g. ``PassthroughAuthResolver`` for ``X-API-Key`` or ``Cookie``),
    with a credential-minting resolver (e.g. ``TokenSourceAuthResolver`` for ``client_credentials``).
    Place the resolver that should win for ``Authorization`` last.
    """

    def __init__(self, resolvers: typing.Sequence[AuthResolver]) -> None:
        self._resolvers = list(resolvers)

    async def resolve(self, ctx: Context) -> dict[str, str]:
        merged: dict[str, str] = {}
        for resolver in self._resolvers:
            merged.update(await resolver.resolve(ctx))
        return merged
