import dataclasses
import logging
import typing

import pydantic
from fastapi import FastAPI

from .auth.detector import DetectedOAuthFlow
from .openapi import OperationInfo
from .settings import AuthConfig


logger = logging.getLogger(__name__)


_TOOL_METADATA_ATTR = '_openapi_mcp_gateway_tool'


class ToolMetadata(pydantic.BaseModel):
    """Overrides recorded on a FastAPI route by ``@mcp_tool``."""

    expose: bool = True
    name: str | None = None
    description: str | None = None


CallableT = typing.TypeVar('CallableT', bound=typing.Callable[..., typing.Any])


def mark_tool(
    func: CallableT,
    *,
    name: str | None = None,
    description: str | None = None,
    expose: bool = True,
) -> CallableT:
    """Imperative version of ``@mcp_tool`` for routes you cannot decorate at definition.

    Use when the route lives in code you do not own (third-party FastAPI app,
    routes pulled in via ``include_router`` from another package, dynamically
    registered endpoints) but you still want to expose it as an MCP tool.

    Equivalent to applying ``@mcp_tool(name=..., description=..., expose=...)``
    to ``func`` after the fact. Returns ``func`` so it can be chained or used
    inside a comprehension.
    """
    metadata = ToolMetadata(name=name, description=description, expose=expose)
    setattr(func, _TOOL_METADATA_ATTR, metadata)
    return func


def mcp_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    expose: bool = True,
) -> typing.Callable[[CallableT], CallableT]:
    """Mark a FastAPI route for exposure through ``Gateway.from_fastapi``.

    Routes without ``@mcp_tool`` are ignored.
    ``name`` and ``description`` override the OpenAPI-derived tool name and description.
    ``expose=False`` opts out without removing the decorator.
    """

    def decorator(func: CallableT) -> CallableT:
        return mark_tool(func, name=name, description=description, expose=expose)

    return decorator


@dataclasses.dataclass(frozen=True)
class RouteSelection:
    """One ``@mcp_tool``-marked FastAPI route paired with its decorator metadata."""

    method: str
    path: str
    metadata: ToolMetadata


# OPTIONS (CORS preflight) and HEAD (mirror of GET) are never meaningful for an LLM to invoke.
_TOOL_HTTP_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')


def collect_marked_routes(app: FastAPI) -> list[RouteSelection]:
    """Return one ``RouteSelection`` per (method, path) for every ``@mcp_tool(expose=True)`` route."""
    selections: list[RouteSelection] = []
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        if endpoint is None:
            continue
        metadata = getattr(endpoint, _TOOL_METADATA_ATTR, None)
        if metadata is None or not metadata.expose:
            continue
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None) or set()
        if path is None:
            continue
        for method in methods:
            if method in _TOOL_HTTP_METHODS:
                selections.append(
                    RouteSelection(
                        method=method.lower(),
                        path=path,
                        metadata=metadata,
                    )
                )
    return selections


def get_tool_metadata(func: typing.Callable[..., typing.Any]) -> ToolMetadata | None:
    """Return the ``ToolMetadata`` stored on ``func`` by ``@mcp_tool``, or ``None``."""
    return getattr(func, _TOOL_METADATA_ATTR, None)


def filter_marked_operations(
    operations: list[OperationInfo],
    selections: list[RouteSelection],
) -> list[tuple[OperationInfo, ToolMetadata]]:
    """Keep operations whose ``(method, path)`` is in ``selections``, paired with their metadata."""
    selection_by_key = {(selection.method, selection.path): selection for selection in selections}
    paired: list[tuple[OperationInfo, ToolMetadata]] = []
    for operation in operations:
        selection = selection_by_key.get((operation.method, operation.path))
        if selection is None:
            continue
        paired.append((operation, selection.metadata))
    return paired


def override_with_metadata(operation: OperationInfo, metadata: ToolMetadata) -> OperationInfo:
    """Apply ``metadata.name`` / ``description`` to ``operation``.

    ``name`` rewrites ``operation_id``, which is the source of the downstream tool name.
    """
    updates: dict[str, typing.Any] = {}
    if metadata.name:
        updates['operation_id'] = metadata.name
    if metadata.description:
        updates['description'] = metadata.description
    return operation.model_copy(update=updates) if updates else operation


def infer_auth_from_declared_flows(declared: list[DetectedOAuthFlow]) -> AuthConfig:
    """Default to ``oauth2`` when the spec declares any supported flow; ``none`` otherwise."""
    if declared:
        return AuthConfig(type='oauth2')
    return AuthConfig(type='none')


def warn_on_mixed_security_schemes(server_name: str, operations: list[OperationInfo]) -> None:
    """Warn when marked operations span multiple security schemes; one resolver may not cover all routes."""
    scheme_names: set[str] = set()
    for operation in operations:
        for security_requirement in operation.security:
            scheme_names.update(security_requirement.keys())
    if len(scheme_names) > 1:
        logger.warning(
            'FastAPI server "%s" exposes routes with %d distinct security schemes: %s. '
            'The gateway uses one auth resolver and one passthrough_headers list for the whole server; '
            'routes whose schemes are not covered may fail authentication. '
            'Consider splitting into separate gateways or extending passthrough_headers.',
            server_name,
            len(scheme_names),
            sorted(scheme_names),
        )
