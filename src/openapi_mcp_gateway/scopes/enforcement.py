import abc
import collections.abc
import typing

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_NAME_HEADER, MCP_PROTOCOL_VERSION_HEADER, decode_header_value
from mcp.types import CallToolResult, TextContent
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .access import OperationAccess, granted_scopes
from .requirement import ScopeRequirement


class CallTarget(abc.ABC):
    """Which registered name a ``tools/call`` addresses, read from the 2026-07-28 request headers alone.

    The SDK rejects a header that disagrees with the body, so the header can be trusted without parsing the body.
    """

    @abc.abstractmethod
    def resolve(self, headers: collections.abc.Mapping[str, str]) -> str | None:
        """Return the name whose scope requirement applies, or ``None`` when the call addresses no guarded name."""


class ToolNameTarget(CallTarget):
    """The tool itself, named in ``Mcp-Name``, for static exposure where each operation is its own tool."""

    def resolve(self, headers: collections.abc.Mapping[str, str]) -> str | None:
        return decode_header_value(headers.get(MCP_NAME_HEADER))


class ScopeChallengeGate:
    """ASGI middleware refusing a ``tools/call`` the token's scopes do not cover, with ``403`` and a scope challenge.

    It runs after the SDK authenticates the request and before the SDK reads the body,
    since only an HTTP response can carry the ``insufficient_scope`` challenge a client steps up from.
    Requests from clients older than 2026-07-28 carry no routing headers and pass through,
    to be refused by ``ScopeFilterMiddleware`` instead.
    """

    def __init__(
        self,
        app: ASGIApp,
        access: OperationAccess,
        call_target: CallTarget,
        resource_metadata_url: str | None,
    ) -> None:
        self.app = app
        self._access = access
        self._call_target = call_target
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
        """The requirement an authenticated 2026-07-28 ``tools/call`` fails, or ``None`` to let the request through.

        An unauthenticated request passes, so the SDK answers it with its own ``401``.
        """
        user = scope.get('user')
        if scope['type'] != 'http' or not isinstance(user, AuthenticatedUser):
            return None
        headers = Headers(scope=scope)
        if headers.get(MCP_PROTOCOL_VERSION_HEADER) not in MODERN_PROTOCOL_VERSIONS:
            return None
        if headers.get(MCP_METHOD_HEADER) != 'tools/call':
            return None
        name = self._call_target.resolve(headers)
        if name is None:
            return None
        requirement = self._access.requirement(name)
        return None if requirement.is_met(frozenset(user.scopes)) else requirement

    def _challenge(self, requirement: ScopeRequirement) -> str:
        """The RFC 6750 ``WWW-Authenticate`` value, naming every scope that would satisfy the call at once."""
        parameters = {
            'error': 'insufficient_scope',
            'scope': ' '.join(requirement.challenge),
            'resource_metadata': self._resource_metadata_url,
            'error_description': requirement.refusal,
        }
        return 'Bearer ' + ', '.join(f'{key}="{value}"' for key, value in parameters.items() if value)


class ScopeFilterMiddleware:
    """SDK middleware hiding tools the token's scopes do not cover, and refusing calls to them.

    The refusal is a tool error, which a client cannot step up from.
    It is the deprecated path for clients older than 2026-07-28, which ``ScopeChallengeGate`` cannot read,
    and goes away with legacy protocol support.
    """

    def __init__(self, access: OperationAccess) -> None:
        self._access = access

    async def __call__(self, ctx: ServerRequestContext[typing.Any, typing.Any], call_next: CallNext) -> HandlerResult:
        if ctx.method == 'tools/call':
            requirement = self._access.requirement(str((ctx.params or {}).get('name', '')))
            if not requirement.is_met(granted_scopes()):
                return CallToolResult(content=[TextContent(type='text', text=requirement.refusal)], is_error=True)
        # Handlers return the wire form by the time middleware sees it, so the list is filtered as JSON.
        result = await call_next(ctx)
        if ctx.method == 'tools/list' and isinstance(result, dict):
            granted = granted_scopes()
            return {
                **result,
                'tools': [tool for tool in result['tools'] if self._access.permits(tool['name'], granted)],
            }
        return result
