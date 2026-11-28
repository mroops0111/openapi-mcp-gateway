import dataclasses
import json
import typing

import httpx
import pydantic
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ContentBlock, InputRequiredResult, TextContent

from ..auth.upstream.reauthorization import UpstreamNoReauthorizer, UpstreamReauthorizer
from ..auth.upstream.resolver import (
    UPSTREAM_CREDENTIAL_UNAVAILABLE,
    UpstreamAuthResolver,
    UpstreamNoCredentialAuthResolver,
)
from ..openapi import OperationInfo, ParameterInfo
from ..upstream_http import UpstreamBinaryBody, UpstreamSharedHttpClient
from ._content import binary_content
from ._media import MediaPromoter
from ._progress import CallProgress
from ._query_string import (
    FormQuerySerializer,
    QueryPair,
    QuerySerializer,
    UndeclaredQuerySerializer,
    encode_query_string,
)
from ._reauthorization import NO_SIGN_IN_POLICY, UpstreamSignInPolicy, send_with_renewal
from ._shared import _get_override, _sanitize_name, _split_by_location
from ._transform import TransformError, apply_transform, compile_expression


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

    1. **Validate**: with ``input_schema``, the schema a tool advertises, the arguments are checked against it.
    2. **Build**: a ``request`` JSONata expression in the ``tool`` override builds the whole upstream request,
       otherwise the arguments pass through by location as declared in the spec.
    3. **Send**: an upstream that says the user's token expired gets the token renewed and the request sent once more.
       ``sign_in_policy`` decides whether a refusal a renewal cannot cure asks the client
       to send the user to sign in again, and it never does by default.
    4. **Shape**: media is promoted out of a successful body, and a ``response`` expression reshapes what is left.

    A call takes the sanitised parameter names as keyword arguments,
    plus ``ctx`` for the MCPServer-injected :class:`Context`.

    ``spec_operation`` is the operation as the spec declares it, before shaping, and defaults to ``operation``.
    The query string follows its parameters, since shaping changes what the LLM sees, not what the upstream takes.
    """

    # The serializer behind each query style the parser keeps.
    # A new style is a new ``QuerySerializer`` here and a new entry in ``openapi``'s sendable query styles.
    _QUERY_SERIALIZERS_BY_STYLE: typing.ClassVar[dict[str, QuerySerializer]] = {
        'form': FormQuerySerializer(),
    }
    # The serializer of every key a request expression adds that no query parameter of the spec declares.
    _UNDECLARED_QUERY_SERIALIZER: typing.ClassVar[QuerySerializer] = UndeclaredQuerySerializer()

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
        path_parameters, query_parameters, header_parameters, body_parameters = _split_by_location(operation.parameters)
        self._path_parameters = _parameters_keyed_by_sanitised_name(path_parameters)
        self._query_parameters = _parameters_keyed_by_sanitised_name(query_parameters)
        self._header_parameters = _parameters_keyed_by_sanitised_name(header_parameters)
        self._body_parameters = _parameters_keyed_by_sanitised_name(body_parameters)
        _, spec_query_parameters, _, _ = _split_by_location((spec_operation or operation).parameters)
        # The spec's own query parameters, by spec name, whose style and explode the query string follows.
        self._spec_query_parameters = {parameter.name: parameter for parameter in spec_query_parameters}
        # The spec name of each header and query parameter, under its spec name and its sanitised name,
        # since a ``request`` expression may name it by either.
        self._header_spec_names = _spec_names_by_either_name(header_parameters)
        self._query_spec_names = _spec_names_by_either_name(spec_query_parameters)
        self._injected_defaults = {
            _sanitize_name(parameter.name): parameter.schema_['default']
            for parameter in operation.parameters
            if parameter.send_default
        }
        tool_override = _get_override(operation, 'tool')
        self._request_transform = (
            compile_expression(tool_override.request, label=f'{operation.operation_id} request')
            if tool_override and tool_override.request
            else None
        )
        self._response_transform = (
            compile_expression(tool_override.response, label=f'{operation.operation_id} response')
            if tool_override and tool_override.response
            else None
        )
        self._validator = (
            Draft202012Validator(input_schema, format_checker=FormatChecker()) if input_schema is not None else None
        )
        # The override wins per path, so an author can correct or add to what the spec declares.
        self._media_promoter = MediaPromoter(operation.response_media, tool_override.media if tool_override else {})

    async def __call__(self, **kwargs: typing.Any) -> CallToolResult | InputRequiredResult:
        context: Context = kwargs.pop('ctx')
        invalid = self._validate(kwargs)
        if invalid is not None:
            return invalid
        arguments = self._with_defaults(kwargs)

        progress = CallProgress(context)
        unsent = await self._sign_in_policy.await_answer(context, self._connection.reauthorizer, progress)
        if unsent is not None:
            return unsent
        await progress.report(f'Sending request to {self._method.upper()} {self._path} ...')

        request = self._build_request(arguments)
        if isinstance(request, CallToolResult):
            return request

        try:
            body = await send_with_renewal(lambda: self._send(context, request), self._connection.reauthorizer)
        except httpx.HTTPStatusError as exception:
            await progress.finish('Request completed (upstream error)')
            offered = await self._sign_in_policy.ask(context, self._connection.reauthorizer, exception.response)
            if offered is not None:
                return offered
            return _build_http_error_result(exception)
        except httpx.RequestError as exception:
            await progress.finish('Request failed (network error)')
            return _build_network_error_result(exception)

        await progress.finish('Request completed')
        return self._shape(body, request)

    def _validate(self, kwargs: dict[str, typing.Any]) -> CallToolResult | None:
        if self._validator is None:
            return None
        provided = _to_jsonable({name: value for name, value in kwargs.items() if value is not None})
        errors = sorted(self._validator.iter_errors(provided), key=lambda error: list(error.path))
        return _build_validation_error_result(errors) if errors else None

    def _with_defaults(self, kwargs: dict[str, typing.Any]) -> dict[str, typing.Any]:
        """Add each x-mcp default the LLM omitted, since it is sent upstream only then."""
        missing = {name: default for name, default in self._injected_defaults.items() if kwargs.get(name) is None}
        return {**kwargs, **missing}

    def _build_request(self, arguments: dict[str, typing.Any]) -> '_UpstreamRequest | CallToolResult':
        """Build the upstream request, or the error result of a failed ``request`` transform or query."""
        if self._request_transform is not None:
            provided = _to_jsonable({name: value for name, value in arguments.items() if value is not None})
            try:
                upstream_object = apply_transform(self._request_transform, provided)
            except TransformError as error:
                return _build_transform_error_result('request', error)
            path, query_arguments, headers, body = _route_upstream_object(
                upstream_object or {}, self._path, self._method, self._header_spec_names, self._query_spec_names
            )
            query_string = self._encode_query_string(query_arguments)
            if isinstance(query_string, CallToolResult):
                return query_string
            return _UpstreamRequest(path, query_string, headers, body)
        path = self._path
        for parameter_name, parameter in self._path_parameters.items():
            value = arguments.get(parameter_name)
            if value is not None:
                path = path.replace(f'{{{parameter.name}}}', str(value))
        headers = {
            name: str(value)
            for name, value in _kwargs_to_upstream_arguments(arguments, self._header_parameters).items()
        }
        query_string = self._encode_query_string(_kwargs_to_upstream_arguments(arguments, self._query_parameters))
        if isinstance(query_string, CallToolResult):
            return query_string
        return _UpstreamRequest(
            path=path,
            query_string=query_string,
            headers=headers,
            body=_to_jsonable(_kwargs_to_upstream_arguments(arguments, self._body_parameters)),
        )

    def _encode_query_string(self, query_arguments: dict[str, typing.Any]) -> str | CallToolResult:
        """Encode query arguments keyed by spec name as the query string, without the leading ``?``.

        An exploded object spreads into its own property names,
        so one that names another query parameter is refused rather than allowed to override it.
        """
        query_pairs: list[QueryPair] = []
        overriding_properties: list[str] = []
        for name, value in _to_jsonable(query_arguments).items():
            for pair in self._query_pairs(name, value):
                if pair.name != name and pair.name in self._spec_query_parameters:
                    overriding_properties.append(f'{name}.{pair.name}')
                query_pairs.append(pair)
        if overriding_properties:
            return _build_query_override_error_result(overriding_properties)
        return encode_query_string(query_pairs)

    def _query_pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        """Return the pairs ``value`` is sent as, in the style and explode of the spec parameter ``name``."""
        parameter = self._spec_query_parameters.get(name)
        if parameter is None:
            # No parameter declares the key, so it takes ``form``'s default, which explodes.
            return self._UNDECLARED_QUERY_SERIALIZER.pairs(name, value, explode=True)
        return self._QUERY_SERIALIZERS_BY_STYLE[parameter.style].pairs(name, value, explode=parameter.explode)

    async def _send(self, context: Context, request: '_UpstreamRequest') -> typing.Any:
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

    def _shape(self, body: typing.Any, request: '_UpstreamRequest') -> CallToolResult:
        # A response transform reshapes JSON, so it has nothing to apply to in a binary body.
        if isinstance(body, UpstreamBinaryBody):
            return _build_binary_result(body)
        # Media paths name fields of the upstream response, so they are promoted before a transform reshapes it.
        url = f'{self._connection.base_url.rstrip("/")}{request.path}'
        body, media_contents = self._media_promoter.promote(body, url)
        if self._response_transform is not None:
            try:
                body = apply_transform(self._response_transform, body)
            except TransformError as error:
                return _build_transform_error_result('response', error)
        return _build_success_result(body, media_contents)


def _build_success_result(payload: typing.Any, media_contents: typing.Sequence[ContentBlock] = ()) -> CallToolResult:
    """Wrap an upstream success body as a ``CallToolResult``.

    Object-shaped JSON also lands in ``structuredContent``.
    Lists and scalars stay text-only since ``structuredContent`` is object-typed in the MCP spec.
    Media promoted out of the body follows the text, in the order its placeholders number it.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    structured_content = payload if isinstance(payload, dict) else None
    return CallToolResult(
        content=[TextContent(type='text', text=text), *media_contents],
        structured_content=structured_content,
        is_error=False,
    )


def _build_binary_result(body: UpstreamBinaryBody) -> CallToolResult:
    """Wrap a binary upstream body as the one content block its media type calls for."""
    return CallToolResult(content=[binary_content(body.content, body.media_type, body.url)], is_error=False)


def _parse_json_object(text: str, content_type: str) -> dict[str, typing.Any] | None:
    """Decode ``text`` as a JSON object when ``content_type`` declares JSON.

    Returns ``None`` when the content type is not JSON, the body is empty,
    the body is not valid JSON, or the parsed value is not an object.
    """
    if 'application/json' not in content_type or not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_http_error_result(exception: httpx.HTTPStatusError) -> CallToolResult:
    """Wrap an upstream non-2xx as an ``isError`` ``CallToolResult``.

    Object-shaped JSON error bodies also land in ``structuredContent``,
    so the client retains structured access to fields like error codes or rate-limit hints.
    ``outputSchema`` does not apply when ``isError`` is true.
    """
    response = exception.response
    body_text = response.text
    structured_content = _parse_json_object(body_text, response.headers.get('content-type', ''))
    request = exception.request
    message = f'Upstream {request.method} {request.url} returned {response.status_code} {response.reason_phrase}:\n{body_text}'
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structured_content=structured_content,
        is_error=True,
    )


def _build_network_error_result(exception: httpx.RequestError) -> CallToolResult:
    """Wrap an httpx transport failure (such as a connect, timeout, or DNS error) as an ``isError`` result.

    No ``response`` is available, so ``structuredContent`` stays ``None``,
    and the message surfaces the exception type for diagnosis.
    """
    request = exception.request
    message = f'Upstream {request.method} {request.url} failed: {type(exception).__name__}: {exception}'
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structured_content=None,
        is_error=True,
    )


def _build_validation_error_result(errors: list[ValidationError]) -> CallToolResult:
    """Wrap input-schema validation failures as an ``is_error`` result, raised before any upstream call.

    The validator runs against the same schema the tool advertises,
    so what the client is shown is exactly what gets enforced.
    The text carries a human-readable summary and ``structuredContent`` carries the same failures,
    keyed by field path and violated keyword, for a client that parses errors programmatically.
    """
    details = [
        {
            'path': '/'.join(str(part) for part in error.path),
            'keyword': str(error.validator),
            'message': error.message,
        }
        for error in errors
    ]
    lines = [f'{detail["path"] or "(root)"}: {detail["message"]}' for detail in details]
    message = 'Input does not satisfy the tool schema:\n' + '\n'.join(lines)
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structured_content={'errors': details},
        is_error=True,
    )


def _build_transform_error_result(stage: str, error: TransformError) -> CallToolResult:
    """Wrap a failed ``request`` or ``response`` JSONata evaluation as an ``is_error`` result.

    ``stage`` is ``'request'`` or ``'response'``, so the client can tell which side of the call broke.
    """
    message = f'The {stage} transform failed: {error}'
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structured_content=None,
        is_error=True,
    )


def _build_query_override_error_result(overriding_properties: list[str]) -> CallToolResult:
    """Wrap object properties that would override other query parameters as an ``is_error`` result.

    ``overriding_properties`` names each as ``parameter.property``, raised before any upstream call.
    """
    message = (
        'These object properties are sent as query parameters of their own, '
        f'and would override the parameters they name: {", ".join(overriding_properties)}'
    )
    return CallToolResult(content=[TextContent(type='text', text=message)], structured_content=None, is_error=True)


def _parameters_keyed_by_sanitised_name(parameters: list[ParameterInfo]) -> dict[str, ParameterInfo]:
    """Build a lookup from sanitised parameter name to the original :class:`ParameterInfo`."""
    return {_sanitize_name(parameter.name): parameter for parameter in parameters}


def _to_jsonable(value: typing.Any) -> typing.Any:
    """Recursively convert pydantic models into plain JSON-serialisable structures.

    ``_schema_to_python_type`` turns an object-shaped body parameter into a dynamic pydantic model,
    so the value reaching here may be a model, a list of models, or a dict whose values are models.
    httpx serialises the body with ``json.dumps``, which cannot encode a model, so each one is dumped first.
    ``exclude_none`` mirrors the old behaviour, where an omitted optional field was absent rather than null.
    """
    if isinstance(value, pydantic.BaseModel):
        return value.model_dump(mode='json', exclude_none=True)
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _kwargs_to_upstream_arguments(
    kwargs: dict[str, typing.Any],
    parameters_by_name: dict[str, ParameterInfo],
) -> dict[str, typing.Any]:
    """Pick non-``None`` ``kwargs`` matching ``parameters_by_name``, keyed by the original API name.

    Used on the pass-through path, when no ``request`` JSONata expression is set,
    to assemble the query / header / body argument dicts for the upstream HTTP call.
    """
    return {
        parameter.name: kwargs[parameter_name]
        for parameter_name, parameter in parameters_by_name.items()
        if kwargs.get(parameter_name) is not None
    }


def _spec_names_by_either_name(parameters: list[ParameterInfo]) -> dict[str, str]:
    """Map each parameter's spec name and sanitised name to its spec name."""
    return {key: parameter.name for parameter in parameters for key in (_sanitize_name(parameter.name), parameter.name)}


def _route_upstream_object(
    upstream_object: dict[str, typing.Any],
    path: str,
    method: str,
    header_spec_names: dict[str, str],
    query_spec_names: dict[str, str],
) -> tuple[str, dict[str, typing.Any], dict[str, str], dict[str, typing.Any]]:
    """Route a ``request`` JSONata result into ``(resolved_path, query_arguments, header_arguments, body_arguments)``.

    A key that names a path-template placeholder fills the path.
    A key that ``header_spec_names`` or ``query_spec_names`` maps, by spec or sanitised name,
    becomes that header or query parameter under its spec name.
    Every other key becomes a query parameter for a body-less method (GET, DELETE, HEAD),
    or a JSON body field otherwise.
    ``None`` values are dropped, so an omitted friendly argument leaves no trace upstream.
    """
    resolved_path = path
    query_arguments: dict[str, typing.Any] = {}
    header_arguments: dict[str, str] = {}
    body_arguments: dict[str, typing.Any] = {}
    method_has_body = method.lower() in ('post', 'put', 'patch')
    for key, value in upstream_object.items():
        if value is None:
            continue
        placeholder = '{' + key + '}'
        if placeholder in resolved_path:
            resolved_path = resolved_path.replace(placeholder, str(value))
        elif key in header_spec_names:
            header_arguments[header_spec_names[key]] = str(value)
        elif key in query_spec_names:
            query_arguments[query_spec_names[key]] = value
        elif method_has_body:
            body_arguments[key] = value
        else:
            query_arguments[key] = value
    return resolved_path, query_arguments, header_arguments, body_arguments


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


@dataclasses.dataclass(frozen=True)
class _UpstreamRequest:
    """One upstream request, built from a call's arguments and ready to send."""

    path: str
    # The encoded query string, without the leading ``?``.
    query_string: str
    headers: dict[str, str]
    body: typing.Any
