import dataclasses
import inspect
import json
import keyword
import logging
import re
import typing

import httpx
import inflection
import pydantic
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .auth.resolver import AuthResolver, NullAuthResolver
from .client import APIClient
from .openapi import OperationInfo, ParameterInfo


logger = logging.getLogger(__name__)


_INVALID_IDENT_CHARS = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_name(name: str) -> str:
    """Coerce ``name`` to a valid Python identifier (digit prefix, keyword suffix)."""
    sanitized = _INVALID_IDENT_CHARS.sub('_', name)
    if sanitized and sanitized[0].isdigit():
        sanitized = '_' + sanitized
    if keyword.iskeyword(sanitized):
        sanitized += '_'
    return sanitized


def _schema_to_python_type(schema: dict[str, typing.Any]) -> typing.Any:
    """Map a JSON Schema fragment to a Python type annotation.

    Resolves ``oneOf`` / ``anyOf`` first since union fragments often omit ``type``.
    A fragment with neither resolves to ``typing.Any``.
    """
    variants = schema.get('oneOf') or schema.get('anyOf')
    if variants:
        types = tuple(_schema_to_python_type(variant) for variant in variants)
        if len(types) == 1:
            return types[0]
        return typing.Union[types]  # type: ignore[valid-type]

    schema_type = schema.get('type')
    if schema_type is None:
        return typing.Any

    if schema_type == 'string':
        return str
    if schema_type == 'integer':
        return int
    if schema_type == 'number':
        return float
    if schema_type == 'boolean':
        return bool
    if schema_type == 'array':
        items = schema.get('items', {})
        item_type = _schema_to_python_type(items)
        return list[item_type]
    if schema_type == 'object':
        return dict[str, typing.Any]
    return typing.Any


def _split_by_location(
    parameters: list[ParameterInfo],
) -> tuple[list[ParameterInfo], list[ParameterInfo], list[ParameterInfo], list[ParameterInfo]]:
    """Bucket ``parameters`` into ``(path, query, header, body)`` lists in spec order."""
    return (
        [p for p in parameters if p.location == 'path'],
        [p for p in parameters if p.location == 'query'],
        [p for p in parameters if p.location == 'header'],
        [p for p in parameters if p.location == 'body'],
    )


@dataclasses.dataclass(frozen=True)
class UpstreamBinding:
    """Per-server HTTP and auth context shared by both exposure strategies."""

    base_url: str
    auth_resolver: AuthResolver = dataclasses.field(default_factory=NullAuthResolver)
    timeout: float = 90
    transport: httpx.AsyncBaseTransport | None = None


def derive_tool_name(operation: OperationInfo) -> str:
    """Return the MCP tool name for ``operation``.

    Honors ``x-mcp-integration.expose.tool.name`` when set,
    otherwise falls back to ``operationId`` underscored and sanitised.
    """
    override = operation.x_mcp_integration.expose.tool if operation.x_mcp_integration.expose else None
    if override and override.name:
        return _sanitize_name(override.name)
    return _sanitize_name(inflection.underscore(operation.operation_id))


def derive_description(operation: OperationInfo) -> str:
    """Return the MCP tool description for ``operation``.

    Honors ``x-mcp-integration.expose.tool.description`` when set,
    otherwise falls back to description, then summary, then ``METHOD /path``.
    """
    override = operation.x_mcp_integration.expose.tool if operation.x_mcp_integration.expose else None
    if override and override.description:
        return override.description
    return operation.description or operation.summary or f'{operation.method.upper()} {operation.path}'


def derive_title(operation: OperationInfo) -> str | None:
    """Return the MCP tool title for ``operation``, or ``None`` to omit the field.

    Uses OpenAPI ``summary`` when present.
    """
    return operation.summary or None


def derive_annotations(operation: OperationInfo) -> ToolAnnotations:
    """Derive MCP ``ToolAnnotations`` for ``operation`` from its HTTP method.

    ``GET`` is read-only and idempotent,
    ``PUT`` / ``PATCH`` / ``DELETE`` are idempotent,
    ``DELETE`` is additionally destructive,
    and every tool is open-world.
    ``title`` mirrors ``Tool.title`` for clients still reading the legacy annotations field.
    """
    method = operation.method.lower()
    return ToolAnnotations(
        title=operation.summary or None,
        readOnlyHint=(method == 'get') or None,
        destructiveHint=(method == 'delete') or None,
        idempotentHint=(method in {'get', 'put', 'patch', 'delete'}) or None,
        openWorldHint=True,
    )


def build_input_schema(operation: OperationInfo) -> dict[str, typing.Any]:
    """Build the JSON Schema describing ``operation`` inputs.

    Dedupes properties by sanitised name and only emits ``required``
    when at least one parameter is required.
    """
    properties: dict[str, typing.Any] = {}
    required: list[str] = []
    seen: set[str] = set()
    for param in operation.parameters:
        pname = _sanitize_name(param.name)
        if pname in seen:
            continue
        seen.add(pname)
        prop_schema = dict(param.schema_) if param.schema_ else {'type': param.schema_type}
        if param.description and 'description' not in prop_schema:
            prop_schema['description'] = param.description
        properties[pname] = prop_schema
        if param.required:
            required.append(pname)
    schema: dict[str, typing.Any] = {'type': 'object', 'properties': properties}
    if required:
        schema['required'] = required
    return schema


def _build_success_result(payload: typing.Any) -> CallToolResult:
    """Wrap an upstream success body as a ``CallToolResult``.

    Object-shaped JSON also lands in ``structuredContent``;
    lists and scalars stay text-only since ``structuredContent`` is object-typed in the MCP spec.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    structured_content = payload if isinstance(payload, dict) else None
    return CallToolResult(
        content=[TextContent(type='text', text=text)],
        structuredContent=structured_content,
        isError=False,
    )


def _parse_json_object(text: str, content_type: str) -> dict[str, typing.Any] | None:
    """Decode ``text`` as a JSON object when ``content_type`` declares JSON.

    Returns ``None`` when the content type is not JSON,
    the body is empty,
    the body is not valid JSON,
    or the parsed value is not an object.
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
        structuredContent=structured_content,
        isError=True,
    )


def _build_network_error_result(exception: httpx.RequestError) -> CallToolResult:
    """Wrap an httpx transport failure (connect, timeout, DNS, etc.) as an ``isError`` result.

    No ``response`` is available,
    so ``structuredContent`` stays ``None``,
    and the message surfaces the exception type for diagnosis.
    """
    request = exception.request
    message = f'Upstream {request.method} {request.url} failed: {type(exception).__name__}: {exception}'
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structuredContent=None,
        isError=True,
    )


def _build_tool_closure(
    method: str,
    path: str,
    path_params: list[ParameterInfo],
    query_params: list[ParameterInfo],
    header_params: list[ParameterInfo],
    body_params: list[ParameterInfo],
    binding: UpstreamBinding,
) -> typing.Callable[..., typing.Awaitable[CallToolResult]]:
    """Build the async callable that issues one upstream request per invocation."""
    path_param_by_pname = {_sanitize_name(p.name): p for p in path_params}
    query_param_by_pname = {_sanitize_name(p.name): p for p in query_params}
    header_param_by_pname = {_sanitize_name(p.name): p for p in header_params}
    body_param_by_pname = {_sanitize_name(p.name): p for p in body_params}

    async def tool_function(**kwargs: typing.Any) -> CallToolResult:
        ctx: Context = kwargs.pop('ctx')
        await ctx.report_progress(0, 1, f'Sending request to {method.upper()} {path} ...')

        auth_headers = await binding.auth_resolver.resolve(ctx)

        resolved_path = path
        for pname, param_info in path_param_by_pname.items():
            value = kwargs.get(pname)
            if value is None:
                continue
            resolved_path = resolved_path.replace(f'{{{param_info.name}}}', str(value))

        query_args: dict[str, typing.Any] = {
            param_info.name: kwargs[pname]
            for pname, param_info in query_param_by_pname.items()
            if kwargs.get(pname) is not None
        }
        header_args: dict[str, str] = {
            param_info.name: str(kwargs[pname])
            for pname, param_info in header_param_by_pname.items()
            if kwargs.get(pname) is not None
        }
        body = {
            param_info.name: kwargs[pname]
            for pname, param_info in body_param_by_pname.items()
            if kwargs.get(pname) is not None
        }
        headers: dict[str, str] = {**auth_headers, **header_args}

        async with APIClient(
            base_url=binding.base_url,
            headers=headers,
            timeout=binding.timeout,
            transport=binding.transport,
        ) as client:
            try:
                result = await client.request(
                    method,
                    resolved_path,
                    params=query_args or None,
                    data=body or None,
                )
            except httpx.HTTPStatusError as exception:
                await ctx.report_progress(1, 1, 'Request completed (upstream error)')
                return _build_http_error_result(exception)
            except httpx.RequestError as exception:
                await ctx.report_progress(1, 1, 'Request failed (network error)')
                return _build_network_error_result(exception)

        await ctx.report_progress(1, 1, 'Request completed')
        return _build_success_result(result)

    return tool_function


def _build_tool_signature(
    path_params: list[ParameterInfo],
    query_params: list[ParameterInfo],
    header_params: list[ParameterInfo],
    body_params: list[ParameterInfo],
) -> tuple[inspect.Signature, dict[str, typing.Any]]:
    """Build the ``inspect.Signature`` and annotations FastMCP reads to derive the tool input schema.

    Order is required params first, then the framework-injected ``ctx``,
    then optional params with ``default=None``. Dedupes by sanitised identifier.
    """
    seen_pnames: set[str] = set()
    ordered_params: list[tuple[str, ParameterInfo]] = []
    for p in path_params + query_params + header_params + body_params:
        pname = _sanitize_name(p.name)
        if pname not in seen_pnames:
            seen_pnames.add(pname)
            ordered_params.append((pname, p))

    annotations: dict[str, typing.Any] = {}
    signature_params: list[inspect.Parameter] = []

    for pname, param in ordered_params:
        if not param.required:
            continue
        py_type = _schema_to_python_type(param.schema_)
        ann = typing.Annotated[py_type, pydantic.Field(description=param.description)] if param.description else py_type
        signature_params.append(
            inspect.Parameter(name=pname, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
        )
        annotations[pname] = ann

    signature_params.append(
        inspect.Parameter(name='ctx', kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    )
    annotations['ctx'] = Context

    for pname, param in ordered_params:
        if param.required:
            continue
        py_type = _schema_to_python_type(param.schema_)
        ann = (
            typing.Annotated[py_type | None, pydantic.Field(description=param.description)]
            if param.description
            else py_type | None
        )
        signature_params.append(
            inspect.Parameter(
                name=pname,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=ann,
                default=None,
            )
        )
        annotations[pname] = ann

    return inspect.Signature(signature_params), annotations


def build_tool_callable(operation: OperationInfo, binding: UpstreamBinding) -> typing.Callable:
    """Build the bare async callable for ``operation``, used by dynamic exposure for registry dispatch."""
    path_params, query_params, header_params, body_params = _split_by_location(operation.parameters)
    return _build_tool_closure(
        method=operation.method,
        path=operation.path,
        path_params=path_params,
        query_params=query_params,
        header_params=header_params,
        body_params=body_params,
        binding=binding,
    )


def build_tool_function(operation: OperationInfo, binding: UpstreamBinding) -> typing.Callable:
    """Build the async callable for ``operation`` with ``inspect.Signature`` attached for FastMCP."""
    path_params, query_params, header_params, body_params = _split_by_location(operation.parameters)
    closure = _build_tool_closure(
        method=operation.method,
        path=operation.path,
        path_params=path_params,
        query_params=query_params,
        header_params=header_params,
        body_params=body_params,
        binding=binding,
    )
    signature, annotations = _build_tool_signature(path_params, query_params, header_params, body_params)
    closure.__signature__ = signature
    closure.__annotations__ = annotations
    return closure


class ToolGenerator:
    """Static exposure: register one MCP tool per operation onto ``mcp``."""

    def __init__(self, mcp: FastMCP, binding: UpstreamBinding):
        self.mcp = mcp
        self.binding = binding

    def register(self, operations: list[OperationInfo]) -> None:
        """Declare one MCP tool per ``OperationInfo`` in ``operations``."""
        for operation in operations:
            tool_function = build_tool_function(operation, self.binding)
            name = derive_tool_name(operation)
            description = derive_description(operation)
            title = derive_title(operation)
            annotations = derive_annotations(operation)
            self.mcp.tool(
                name=name,
                title=title,
                description=description,
                annotations=annotations,
            )(tool_function)
            logger.debug('Tool registered: %s ← %s %s', name, operation.method.upper(), operation.path)
        logger.info('Registered %d MCP tool(s) on server "%s"', len(operations), self.mcp.name)


@dataclasses.dataclass
class MetaToolEntry:
    """One operation indexed by the dynamic-exposure registry."""

    description: str
    input_schema: dict[str, typing.Any]
    callable_: typing.Callable[..., typing.Awaitable[CallToolResult]]


_LIST_OPERATIONS_DESCRIPTION = (
    'List every operation available on this server as `{name, description}` entries. '
    'Call `get_operation(name)` next to fetch the input schema for one specific operation.'
)

_GET_OPERATION_DESCRIPTION = (
    'Return the input schema for one operation as `{name, description, input_schema}`. '
    'Use the JSON Schema to construct the `arguments` payload for `call_operation`.'
)

_CALL_OPERATION_DESCRIPTION = (
    'Invoke an operation by name with a JSON object of arguments. '
    'Argument keys must match the keys in `get_operation(name).input_schema.properties`.'
)


def _unknown_operation(name: str) -> ValueError:
    """Build the standard error for ``get_operation`` and ``call_operation`` when ``name`` is not indexed."""
    return ValueError(f'Unknown operation: {name!r}. Use list_operations to see available names.')


class MetaToolGenerator:
    """Dynamic exposure: register ``list_operations`` / ``get_operation`` / ``call_operation`` meta-tools.

    The LLM walks list → get → call to discover and invoke a specific operation.
    Per-op callables come from the same closure the static path uses,
    so auth, path substitution, and request shape stay identical across modes.
    """

    def __init__(self, mcp: FastMCP, binding: UpstreamBinding):
        self.mcp = mcp
        self.binding = binding
        self._registry: dict[str, MetaToolEntry] = {}

    def register(self, operations: list[OperationInfo]) -> None:
        """Populate the registry from ``operations`` and bind the three meta-tools."""
        for operation in operations:
            name = derive_tool_name(operation)
            self._registry[name] = MetaToolEntry(
                description=derive_description(operation),
                input_schema=build_input_schema(operation),
                callable_=build_tool_callable(operation, self.binding),
            )
            logger.debug('Dynamic operation indexed: %s ← %s %s', name, operation.method.upper(), operation.path)
        self._bind_meta_tools()
        logger.info(
            'Registered %d operation(s) behind dynamic meta-tools on server "%s"',
            len(self._registry),
            self.mcp.name,
        )

    def _bind_meta_tools(self) -> None:
        registry = self._registry

        @self.mcp.tool(name='list_operations', description=_LIST_OPERATIONS_DESCRIPTION)
        async def list_operations(ctx: Context) -> str:
            entries = [{'name': name, 'description': entry.description} for name, entry in registry.items()]
            return json.dumps(entries, ensure_ascii=False)

        @self.mcp.tool(name='get_operation', description=_GET_OPERATION_DESCRIPTION)
        async def get_operation(name: str, ctx: Context) -> str:
            entry = registry.get(name)
            if entry is None:
                raise _unknown_operation(name)
            return json.dumps(
                {'name': name, 'description': entry.description, 'input_schema': entry.input_schema},
                ensure_ascii=False,
            )

        @self.mcp.tool(name='call_operation', description=_CALL_OPERATION_DESCRIPTION)
        async def call_operation(name: str, arguments: dict[str, typing.Any], ctx: Context) -> CallToolResult:
            entry = registry.get(name)
            if entry is None:
                raise _unknown_operation(name)
            return await entry.callable_(**arguments, ctx=ctx)
