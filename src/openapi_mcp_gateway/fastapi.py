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
    """MCP-side overrides recorded on a FastAPI route by ``@mcp_tool``."""

    expose: bool = True
    name: str | None = None
    description: str | None = None


CallableT = typing.TypeVar('CallableT', bound=typing.Callable[..., typing.Any])


def mcp_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    expose: bool = True,
) -> typing.Callable[[CallableT], CallableT]:
    """Mark a FastAPI route function for exposure through the MCP gateway.

    Routes without ``@mcp_tool`` are ignored by ``Gateway.from_fastapi``;
    decorate every endpoint you want the LLM to see. ``name`` and
    ``description`` override the OpenAPI-derived tool name/description for
    that single route. ``expose=False`` lets a route opt out without removing
    the decorator.
    """
    metadata = ToolMetadata(name=name, description=description, expose=expose)

    def decorator(func: CallableT) -> CallableT:
        setattr(func, _TOOL_METADATA_ATTR, metadata)
        return func

    return decorator


@dataclasses.dataclass(frozen=True)
class RouteSelection:
    """One FastAPI route exposed via MCP, with its decorator metadata."""

    method: str
    path: str
    metadata: ToolMetadata


# MCP tools represent real upstream operations; OPTIONS (CORS preflight)
# and HEAD (mirror of GET) are excluded because they are never meaningful
# things for an LLM to invoke.
_TOOL_HTTP_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')


def collect_marked_routes(app: FastAPI) -> list[RouteSelection]:
    """Return ``RouteSelection`` for each route decorated with ``@mcp_tool(expose=True)``.

    Multi-method routes (``@app.api_route(['GET','POST'], ...)``) yield one
    entry per HTTP verb so each downstream OpenAPI operation can be matched
    by ``(method, path)``.
    """
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
    """Keep only operations whose ``(method, path)`` appears in ``selections``.

    Returns each surviving operation paired with its decorator metadata so
    callers don't need to look it up again to apply overrides.
    """
    selection_by_key = {(selection.method, selection.path): selection for selection in selections}
    paired: list[tuple[OperationInfo, ToolMetadata]] = []
    for operation in operations:
        selection = selection_by_key.get((operation.method, operation.path))
        if selection is None:
            continue
        paired.append((operation, selection.metadata))
    return paired


def override_with_metadata(operation: OperationInfo, metadata: ToolMetadata) -> OperationInfo:
    """Return a copy of ``operation`` with ``metadata.name`` / ``description`` applied.

    ``name`` rewrites ``operation_id`` because the downstream tool name is
    derived from it. ``description`` replaces the operation description shown
    to the LLM.
    """
    updates: dict[str, typing.Any] = {}
    if metadata.name:
        updates['operation_id'] = metadata.name
    if metadata.description:
        updates['description'] = metadata.description
    return operation.model_copy(update=updates) if updates else operation


def infer_auth_from_declared_flows(declared: list[DetectedOAuthFlow]) -> AuthConfig:
    """Pick a plausible ``AuthConfig`` from spec-declared OAuth2 flows.

    ``oauth2`` is selected when the spec declares any supported flow so the
    factory can choose an ``authorization_code`` / ``client_credentials`` /
    ``passthrough`` resolver. ``none`` is selected when the spec is silent
    about auth, matching the FastAPI app's posture.
    """
    if declared:
        return AuthConfig(type='oauth2')
    return AuthConfig(type='none')


def warn_on_mixed_security_schemes(server_name: str, operations: list[OperationInfo]) -> None:
    """Log a warning when marked operations advertise multiple distinct security schemes.

    The gateway runs one resolver per server and forwards a fixed set of
    headers. Routes using multiple schemes (e.g. Bearer on some endpoints,
    cookies on others) may authenticate inconsistently — surface that at
    startup so the operator can split servers or extend ``passthrough_headers``.
    """
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
