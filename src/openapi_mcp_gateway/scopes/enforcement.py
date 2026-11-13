import abc
import collections.abc
import typing

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_NAME_HEADER, MCP_PROTOCOL_VERSION_HEADER, decode_header_value
from mcp.types import INVALID_REQUEST, CallToolResult, TextContent
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .grant import current_grant
from .registry import ScopeRegistry
from .requirement import ScopeRequirement


class RequestTarget(abc.ABC):
    """Which registered name a request addresses, read from the 2026-07-28 request headers alone.

    The SDK rejects a header that disagrees with the body, so the header can be trusted without parsing the body.
    """

    @abc.abstractmethod
    def resolve(self, headers: collections.abc.Mapping[str, str]) -> str | None:
        """Return the name whose scope requirement applies, or ``None`` when the request addresses no guarded name."""


class NameHeaderTarget(RequestTarget):
    """The name ``Mcp-Name`` carries, which is the tool for ``tools/call`` and the URI for ``resources/read``.

    It is the whole target under static exposure, where each operation is its own tool.
    """

    def resolve(self, headers: collections.abc.Mapping[str, str]) -> str | None:
        return decode_header_value(headers.get(MCP_NAME_HEADER))


class ScopeChallengeMiddleware:
    """ASGI middleware refusing a request the token's scopes do not cover, with ``403`` and a scope challenge.

    It guards ``tools/call``, through the target the exposure style names, and ``resources/read``.
    Only the MCP scopes are challenged, since stepping up at the gateway cannot change what the upstream granted.
    It runs after the SDK authenticates the request and before the SDK reads the body,
    since only an HTTP response can carry the ``insufficient_scope`` challenge a client steps up from.
    Requests from clients older than 2026-07-28 carry no routing headers and pass through,
    to be refused by ``ScopeFilterMiddleware`` instead.
    """

    def __init__(
        self,
        app: ASGIApp,
        scope_registry: ScopeRegistry,
        tool_call_target: RequestTarget,
        resource_metadata_url: str | None,
    ) -> None:
        self.app = app
        self._scope_registry = scope_registry
        self._targets: dict[str, RequestTarget] = {'tools/call': tool_call_target, 'resources/read': NameHeaderTarget()}
        self._resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        unmet = self._unmet_requirement(scope)
        if unmet is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=403,
            content={'error': 'insufficient_scope', 'error_description': unmet.refusal},
            headers={'WWW-Authenticate': self._challenge(unmet)},
        )
        await response(scope, receive, send)

    def _unmet_requirement(self, scope: Scope) -> ScopeRequirement | None:
        """The requirement an authenticated 2026-07-28 request fails, or ``None`` to let the request through.

        An unauthenticated request passes, so the SDK answers it with its own ``401``.
        """
        user = scope.get('user')
        if scope['type'] != 'http' or not isinstance(user, AuthenticatedUser):
            return None
        headers = Headers(scope=scope)
        if headers.get(MCP_PROTOCOL_VERSION_HEADER) not in MODERN_PROTOCOL_VERSIONS:
            return None
        target = self._targets.get(headers.get(MCP_METHOD_HEADER, ''))
        name = target.resolve(headers) if target else None
        if name is None:
            return None
        requirement = self._scope_registry.mcp_requirement(name)
        return None if requirement.is_met(frozenset(user.scopes)) else requirement

    def _challenge(self, requirement: ScopeRequirement) -> str:
        """The RFC 6750 ``WWW-Authenticate`` value, naming every scope that would satisfy the request at once."""
        parameters = {
            'error': 'insufficient_scope',
            'scope': ' '.join(requirement.challenge),
            'resource_metadata': self._resource_metadata_url,
            'error_description': requirement.refusal,
        }
        return 'Bearer ' + ', '.join(f'{key}="{value}"' for key, value in parameters.items() if value)


class ScopeFilterMiddleware:
    """SDK middleware hiding the tools and resources a token may not use, and refusing requests for them.

    A refused tool call is a tool error and a refused resource read a protocol error,
    which a client cannot step up from.
    It is the only refusal of a token whose upstream grant falls short, since stepping up cannot fix that.
    For MCP scopes, it is the deprecated path for clients older than 2026-07-28, which ``ScopeChallengeMiddleware`` cannot read.
    """

    def __init__(self, scope_registry: ScopeRegistry) -> None:
        self._scope_registry = scope_registry

    async def __call__(self, ctx: ServerRequestContext[typing.Any, typing.Any], call_next: CallNext) -> HandlerResult:
        guarded = _GUARDED_REQUESTS.get(ctx.method)
        if guarded is not None:
            refusal = self._scope_registry.refusal(str((ctx.params or {}).get(guarded.name_param, '')), current_grant())
            if refusal is not None:
                return guarded.refuse(refusal)
        # Handlers return the wire form by the time middleware sees it, so a list is filtered as JSON.
        result = await call_next(ctx)
        listing = _LISTINGS.get(ctx.method)
        if listing is not None and isinstance(result, dict):
            grant = current_grant()
            return {
                **result,
                listing.items_key: [
                    item
                    for item in result[listing.items_key]
                    if self._scope_registry.permits(item[listing.name_key], grant)
                ],
            }
        return result


class _GuardedRequest(typing.NamedTuple):
    """A request for one tool or resource, named by a body parameter, and how its refusal is answered."""

    name_param: str
    refuse: typing.Callable[[str], HandlerResult]


class _Listing(typing.NamedTuple):
    """A list result, filtered item by item by the name each item carries."""

    items_key: str
    name_key: str


def _refuse_tool_call(refusal: str) -> HandlerResult:
    """A tool error, the result a tool call that cannot run answers with."""
    return CallToolResult(content=[TextContent(type='text', text=refusal)], is_error=True)


def _refuse_resource_read(refusal: str) -> HandlerResult:
    """A protocol error, since a resource read has no error result of its own."""
    raise MCPError(INVALID_REQUEST, refusal)


# The requests the deprecated path refuses, keyed by method.
_GUARDED_REQUESTS: dict[str, _GuardedRequest] = {
    'tools/call': _GuardedRequest('name', _refuse_tool_call),
    'resources/read': _GuardedRequest('uri', _refuse_resource_read),
}

# The lists filtered by what the token's scopes open, keyed by method.
_LISTINGS: dict[str, _Listing] = {
    'tools/list': _Listing('tools', 'name'),
    'resources/list': _Listing('resources', 'uri'),
    'resources/templates/list': _Listing('resourceTemplates', 'uriTemplate'),
}
