import dataclasses
import typing

import httpx
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult

from ..auth.upstream.reauthorization import UpstreamNoReauthorizer, UpstreamReauthorizer
from ..auth.upstream.resolver import (
    UPSTREAM_CREDENTIAL_UNAVAILABLE,
    UpstreamAuthResolver,
    UpstreamNoCredentialAuthResolver,
)
from ..openapi import OperationInfo
from ._progress import CallProgress
from ._reauthorization import NO_SIGN_IN_POLICY, UpstreamSignInPolicy, send_with_renewal
from ._shaping import RequestShaper, ResponseShaper
from ._tool_result import build_http_error_result, build_network_error_result
from ._upstream_http import UpstreamSharedHttpClient
from ._upstream_request import UpstreamRequest, UpstreamRequestBuilder


# W3C trace-context keys the 2026-07-28 spec carries in a request's ``_meta`` (spec minor #2).
# The gateway sits on the hop between the MCP client and the upstream API,
# so forwarding these verbatim stitches that hop into one distributed trace.
_TRACE_CONTEXT_META_KEYS = ('traceparent', 'tracestate', 'baggage')


@dataclasses.dataclass(frozen=True)
class UpstreamConnection:
    """How one server reaches its upstream API, shared by every exposure strategy.

    It holds where the API is, how each call authenticates, the static headers every call sends,
    and the one HTTP client every call goes through.
    ``reauthorizer`` cures an upstream refusal of the caller's own upstream token, where the flow holds one.
    """

    base_url: str
    auth_resolver: UpstreamAuthResolver = dataclasses.field(default_factory=UpstreamNoCredentialAuthResolver)
    reauthorizer: UpstreamReauthorizer = dataclasses.field(default_factory=UpstreamNoReauthorizer)
    timeout: float = 90
    transport: httpx.AsyncBaseTransport | None = None
    # Static per-server headers, sent on every call beneath the auth and per-call headers.
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    # The one HTTP client every call to this server goes through, built from the fields above.
    client: UpstreamSharedHttpClient = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'client', UpstreamSharedHttpClient(self.base_url, self.timeout, self.transport))


class UpstreamOperationCall:
    """The upstream call behind one operation, shared by every exposure mode.

    Everything that depends only on the operation is prepared once, here,
    so a syntax error in a ``request`` or ``response`` JSONata expression surfaces at startup.
    Each call then runs its steps in order.

    1. **Shape the Request**: :class:`RequestShaper` checks the arguments against ``input_schema``,
       the schema a tool advertises, and turns them into upstream arguments.
    2. **Build the Request**: :class:`UpstreamRequestBuilder` lays them out as the spec declares.
    3. **Send**: an upstream that says the user's token expired gets the token renewed and the request sent once more.
       ``sign_in_policy`` decides whether a refusal a renewal cannot cure asks the client
       to send the user to sign in again, and it never does by default.
    4. **Shape the Response**: :class:`ResponseShaper` turns a successful body into the tool result.

    A call takes the sanitised parameter names as keyword arguments,
    plus ``ctx`` for the MCPServer-injected :class:`Context`.
    Shaping follows ``operation``, and the request follows ``spec_operation``, the operation before shaping,
    which defaults to ``operation``.
    """

    def __init__(
        self,
        operation: OperationInfo,
        connection: UpstreamConnection,
        *,
        input_schema: dict[str, typing.Any] | None = None,
        sign_in_policy: UpstreamSignInPolicy = NO_SIGN_IN_POLICY,
        spec_operation: OperationInfo | None = None,
    ) -> None:
        self._connection = connection
        self._sign_in_policy = sign_in_policy
        self._method = operation.method
        self._path = operation.path
        self._request_shaper = RequestShaper(operation, input_schema)
        self._request_builder = UpstreamRequestBuilder(spec_operation or operation)
        self._response_shaper = ResponseShaper(operation)

    async def __call__(self, **kwargs: typing.Any) -> CallToolResult | InputRequiredResult:
        context: Context = kwargs.pop('ctx')
        upstream_arguments = self._request_shaper.shape_request(kwargs)
        if isinstance(upstream_arguments, CallToolResult):
            return upstream_arguments
        request = self._request_builder.build_request(upstream_arguments)
        if isinstance(request, CallToolResult):
            return request

        progress = CallProgress(context)
        unsent = await self._sign_in_policy.await_answer(context, self._connection.reauthorizer, progress)
        if unsent is not None:
            return unsent
        await progress.report(f'Sending request to {self._method.upper()} {self._path} ...')

        try:
            body = await send_with_renewal(lambda: self._send(context, request), self._connection.reauthorizer)
        except httpx.HTTPStatusError as exception:
            await progress.finish('Request completed (upstream error)')
            offered = await self._sign_in_policy.ask(context, self._connection.reauthorizer, exception.response)
            if offered is not None:
                return offered
            return build_http_error_result(exception)
        except httpx.RequestError as exception:
            await progress.finish('Request failed (network error)')
            return build_network_error_result(exception)

        await progress.finish('Request completed')
        return self._response_shaper.shape_response(body, f'{self._connection.base_url.rstrip("/")}{request.path}')

    async def _send(self, context: Context, request: UpstreamRequest) -> typing.Any:
        connection = self._connection
        # Resolved on each attempt, so a retry carries the token a renewal put in place.
        try:
            auth_headers = await connection.auth_resolver.resolve(context)
        except httpx.HTTPError as exception:
            # Failing to obtain a credential is the gateway's failure, so it is not reported as the API's.
            raise RuntimeError(UPSTREAM_CREDENTIAL_UNAVAILABLE) from exception
        headers = {**connection.headers, **_trace_context_headers(context), **auth_headers, **request.headers}
        # The client is shared by every call to this server, so every header, auth included, rides on the request.
        return await connection.client.get().request(
            self._method, request.path, query_string=request.query_string, data=request.body or None, headers=headers
        )


def _trace_context_headers(context: Context) -> dict[str, str]:
    """Return the W3C trace-context headers to forward upstream, read from the request ``_meta``.

    Returns an empty dict when there is no request context or no trace keys are present,
    so a call that arrives without trace context forwards nothing.
    """
    try:
        meta = context.request_context.meta
    except (AttributeError, ValueError):
        return {}
    if not meta:
        return {}
    return {key: str(value) for key in _TRACE_CONTEXT_META_KEYS if (value := meta.get(key)) is not None}
