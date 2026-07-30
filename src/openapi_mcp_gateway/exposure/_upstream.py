import json
import typing

import httpx
import pydantic
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from ..client import APIClient
from ..openapi import OperationInfo, ParameterInfo
from ._shared import UpstreamBinding, _sanitize_name, _split_by_location


def _build_success_result(payload: typing.Any) -> CallToolResult:
    """Wrap an upstream success body as a ``CallToolResult``.

    Object-shaped JSON also lands in ``structuredContent``.
    Lists and scalars stay text-only since ``structuredContent`` is object-typed in the MCP spec.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    structured_content = payload if isinstance(payload, dict) else None
    return CallToolResult(
        content=[TextContent(type='text', text=text)],
        structured_content=structured_content,
        is_error=False,
    )


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
    """Wrap an httpx transport failure (connect, timeout, DNS, etc.) as an ``isError`` result.

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
    """Pick non-``None`` ``kwargs`` matching ``parameters_by_name``, keyed by the original (un-sanitised) name.

    Used to assemble query / header / body argument dicts for the upstream HTTP call.
    """
    return {
        parameter.name: kwargs[parameter_name]
        for parameter_name, parameter in parameters_by_name.items()
        if kwargs.get(parameter_name) is not None
    }


def _build_upstream_closure(
    operation: OperationInfo,
    binding: UpstreamBinding,
) -> typing.Callable[..., typing.Awaitable[CallToolResult]]:
    """Build the async callable that issues one upstream request per invocation.

    Shared by every exposure mode.
    The callable accepts the sanitised parameter names as keyword arguments,
    plus ``ctx`` for the FastMCP-injected :class:`Context`.
    """
    path_parameters, query_parameters, header_parameters, body_parameters = _split_by_location(operation.parameters)
    path_parameters_by_name = _parameters_keyed_by_sanitised_name(path_parameters)
    query_parameters_by_name = _parameters_keyed_by_sanitised_name(query_parameters)
    header_parameters_by_name = _parameters_keyed_by_sanitised_name(header_parameters)
    body_parameters_by_name = _parameters_keyed_by_sanitised_name(body_parameters)
    method = operation.method
    path = operation.path

    async def upstream_callable(**kwargs: typing.Any) -> CallToolResult:
        context: Context = kwargs.pop('ctx')
        await context.report_progress(0, 1, f'Sending request to {method.upper()} {path} ...')

        auth_headers = await binding.auth_resolver.resolve(context)

        resolved_path = path
        for parameter_name, parameter in path_parameters_by_name.items():
            value = kwargs.get(parameter_name)
            if value is None:
                continue
            resolved_path = resolved_path.replace(f'{{{parameter.name}}}', str(value))

        query_arguments = _kwargs_to_upstream_arguments(kwargs, query_parameters_by_name)
        header_arguments = {
            name: str(value) for name, value in _kwargs_to_upstream_arguments(kwargs, header_parameters_by_name).items()
        }
        body_arguments = _to_jsonable(_kwargs_to_upstream_arguments(kwargs, body_parameters_by_name))
        request_headers: dict[str, str] = {**auth_headers, **header_arguments}

        async with APIClient(
            base_url=binding.base_url,
            headers=request_headers,
            timeout=binding.timeout,
            transport=binding.transport,
        ) as client:
            try:
                result = await client.request(
                    method,
                    resolved_path,
                    params=query_arguments or None,
                    data=body_arguments or None,
                )
            except httpx.HTTPStatusError as exception:
                await context.report_progress(1, 1, 'Request completed (upstream error)')
                return _build_http_error_result(exception)
            except httpx.RequestError as exception:
                await context.report_progress(1, 1, 'Request failed (network error)')
                return _build_network_error_result(exception)

        await context.report_progress(1, 1, 'Request completed')
        return _build_success_result(result)

    return upstream_callable
