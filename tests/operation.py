import collections.abc
import typing

from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import (
    ToolGenerator,
    UpstreamConnection,
)
from openapi_mcp_gateway.openapi import (
    McpIntegration,
    OperationInfo,
    ParameterInfo,
    ParamOverride,
    ResourceOverride,
    ToolOverride,
)
from tests.constants import API_URL


def operation(
    operation_id: str = 'listPets',
    method: str = 'get',
    path: str = '/pets',
    tags: collections.abc.Sequence[str] = ('pets',),
    security: collections.abc.Sequence[dict[str, list[str]]] = (),
) -> OperationInfo:
    """An operation as the spec parser produces it, with only what scopes read from it."""
    return OperationInfo(operation_id=operation_id, method=method, path=path, tags=list(tags), security=list(security))


def shaped_operation(
    parameters: list[ParameterInfo],
    *,
    params: dict | None = None,
    annotations: dict | None = None,
    request: str | None = None,
    response: str | None = None,
    params_strategy: typing.Literal['merge', 'replace'] | None = None,
    method: str = 'get',
    path: str = '/things',
    operation_id: str = 'do_thing',
    summary: str = '',
) -> OperationInfo:
    """Build an operation whose ``x-mcp-integration.tool`` carries the given overrides.

    When ``params`` is set and ``params_strategy`` is not, it defaults to ``replace`` if any entry declares a ``type``,
    else ``merge``, so tests that only care about the override still read cleanly.
    """
    if params and params_strategy is None:
        params_strategy = 'replace' if any('type' in cfg for cfg in params.values()) else 'merge'
    tool = ToolOverride(
        params={name: ParamOverride(**cfg) for name, cfg in (params or {}).items()},
        annotations=annotations,
        params_strategy=params_strategy,
        request=request,
        response=response,
    )
    return OperationInfo(
        operation_id=operation_id,
        method=method,
        path=path,
        summary=summary,
        parameters=parameters,
        x_mcp_integration=McpIntegration(tool=tool),
    )


def register_tool(operation: OperationInfo):
    """Register ``operation`` as a static tool and return the registered Tool named ``do_thing``."""
    mcp = MCPServer('test')
    ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation])
    return next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')


def expose_resource(**kwargs) -> McpIntegration:
    """Build an ``x-mcp-integration`` payload that opts an operation into resource exposure only."""
    return McpIntegration(resource=ResourceOverride(**kwargs))


# The petstore fixture's operations as scopes see them, tagged and untagged alike.
PETSTORE_OPERATIONS = (
    operation('listPets', 'get', '/pets'),
    operation('createPet', 'post', '/pets'),
    operation('getPetById', 'get', '/pets/{petId}'),
    operation('deletePet', 'delete', '/pets/{petId}'),
    operation('createPetWithOwner', 'post', '/pets/with-owner', tags=()),
    operation('adminListPets', 'get', '/admin/pets', tags=()),
)
