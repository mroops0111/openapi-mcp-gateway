import abc
import typing

from mcp.server.mcpserver import Context

from .token_source import UpstreamTokenSource


# What a call is answered with when the gateway fails to obtain its upstream credential.
# The failure is the gateway's, so its detail goes to the log rather than to the caller.
UPSTREAM_CREDENTIAL_UNAVAILABLE = 'The gateway could not obtain its upstream credential'


class UpstreamAuthResolver(abc.ABC):
    """Strategy that produces the upstream HTTP headers for one MCP tool call.

    The contract is header-shaped, not value-shaped.
    Implementations return every header they want on the request, such as ``Authorization`` or ``X-API-Key``,
    and ``ToolGenerator`` merges that dict in unchanged.
    """

    @abc.abstractmethod
    async def resolve(self, ctx: Context) -> dict[str, str]:
        """Return headers to attach to the upstream request, or ``{}`` for none."""


class UpstreamNoCredentialAuthResolver(UpstreamAuthResolver):
    """Contribute no headers, for a public upstream API."""

    async def resolve(self, ctx: Context) -> dict[str, str]:
        return {}


class UpstreamStaticAuthResolver(UpstreamAuthResolver):
    """Fixed credential header captured at construction time.

    Used for ``bearer`` (``Authorization: Bearer ...``) and ``api_key`` (``<api_key_header>: ...``) configs.
    """

    def __init__(self, header_value: str, header_name: str = 'Authorization') -> None:
        self._header_name = header_name
        self._header_value = header_value

    async def resolve(self, ctx: Context) -> dict[str, str]:
        return {self._header_name: self._header_value}


class UpstreamTokenSourceAuthResolver(UpstreamAuthResolver):
    """Send the bearer an ``UpstreamTokenSource`` supplies, or nothing when it has none for this call.

    Every OAuth flow ends here.
    ``client_credentials`` shares one token across all MCP clients,
    while ``authorization_code`` and ``token_exchange`` find the one belonging to the caller.
    """

    def __init__(self, token_source: UpstreamTokenSource) -> None:
        self._token_source = token_source

    async def resolve(self, ctx: Context) -> dict[str, str]:
        token = await self._token_source.get_token()
        if token:
            return {'Authorization': f'Bearer {token}'}
        return {}


class UpstreamPassthroughAuthResolver(UpstreamAuthResolver):
    """Forward selected headers from the live MCP request to the upstream call.

    Used when the gateway and the upstream API share an OAuth realm, typically the FastAPI in-process integration.
    ``header_names`` lists which headers to copy verbatim (case-insensitive).
    The default forwards only ``Authorization``.
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


class UpstreamCompositeAuthResolver(UpstreamAuthResolver):
    """Compose multiple resolvers, with later resolvers overriding earlier ones on key collision.

    Used to combine an incoming-header forwarder, such as ``UpstreamPassthroughAuthResolver`` for ``X-API-Key``,
    with a credential-minting resolver, such as ``UpstreamTokenSourceAuthResolver`` for ``client_credentials``.
    Place the resolver that should win for ``Authorization`` last.
    """

    def __init__(self, resolvers: typing.Sequence[UpstreamAuthResolver]) -> None:
        self._resolvers = list(resolvers)

    async def resolve(self, ctx: Context) -> dict[str, str]:
        merged: dict[str, str] = {}
        for resolver in self._resolvers:
            merged.update(await resolver.resolve(ctx))
        return merged
