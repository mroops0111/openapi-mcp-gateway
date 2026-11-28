import json
import typing

import httpx
from jsonschema import ValidationError
from mcp.types import CallToolResult, ContentBlock, TextContent

from ._binary_content import binary_content
from ._transform import TransformError
from ._upstream_http import UpstreamBinaryBody


def build_success_result(payload: typing.Any, media_contents: typing.Sequence[ContentBlock] = ()) -> CallToolResult:
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


def build_binary_result(body: UpstreamBinaryBody) -> CallToolResult:
    """Wrap a binary upstream body as the one content block its media type calls for."""
    return CallToolResult(content=[binary_content(body.content, body.media_type, body.url)], is_error=False)


def build_http_error_result(exception: httpx.HTTPStatusError) -> CallToolResult:
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


def build_network_error_result(exception: httpx.RequestError) -> CallToolResult:
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


def build_validation_error_result(errors: list[ValidationError]) -> CallToolResult:
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


def build_transform_error_result(stage: str, error: TransformError) -> CallToolResult:
    """Wrap a failed ``request`` or ``response`` JSONata evaluation as an ``is_error`` result.

    ``stage`` is ``'request'`` or ``'response'``, so the client can tell which side of the call broke.
    """
    message = f'The {stage} transform failed: {error}'
    return CallToolResult(
        content=[TextContent(type='text', text=message)],
        structured_content=None,
        is_error=True,
    )


def build_query_override_error_result(overriding_properties: list[str]) -> CallToolResult:
    """Wrap object properties that would override other query parameters as an ``is_error`` result.

    ``overriding_properties`` names each as ``parameter.property``, raised before any upstream call.
    """
    message = (
        'These object properties are sent as query parameters of their own, '
        f'and would override the parameters they name: {", ".join(overriding_properties)}'
    )
    return CallToolResult(content=[TextContent(type='text', text=message)], structured_content=None, is_error=True)


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
