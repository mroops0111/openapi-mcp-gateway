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
from tests.served_tool import ServedTool


def operation(
    operation_id: str = 'listProducts',
    method: str = 'get',
    path: str = '/products',
    tags: collections.abc.Sequence[str] = ('products',),
    security: collections.abc.Sequence[dict[str, list[str]]] = (),
) -> OperationInfo:
    """An operation as the spec parser produces it, with only what scopes read from it."""
    return OperationInfo(operation_id=operation_id, method=method, path=path, tags=list(tags), security=list(security))


def query_parameter(
    name: str = 'opt_fields',
    *,
    schema: dict[str, typing.Any] | None = None,
    explode: bool | None = None,
    style: str | None = None,
) -> ParameterInfo:
    """A query parameter as the spec parser produces it, an array of strings unless ``schema`` says otherwise."""
    return ParameterInfo(
        name=name,
        location='query',
        schema=schema or {'type': 'array', 'items': {'type': 'string'}},
        declared_style=style,
        declared_explode=explode,
    )


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
    unsendable_query_parameters: collections.abc.Sequence[str] = (),
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
        unsendable_query_parameters=list(unsendable_query_parameters),
    )


def register_tool(operation: OperationInfo) -> ServedTool:
    """Register ``operation`` as a static tool, and return the tool named ``do_thing`` as a client reaches it."""
    mcp = MCPServer('test')
    ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation])
    return ServedTool(mcp, 'do_thing')


def expose_resource(**kwargs) -> McpIntegration:
    """Build an ``x-mcp-integration`` payload that opts an operation into resource exposure only."""
    return McpIntegration(resource=ResourceOverride(**kwargs))


# The shop fixture's operations as scopes see them, tagged and untagged alike.
SHOP_OPERATIONS = (
    operation('listProducts', 'get', '/products'),
    operation('createProduct', 'post', '/products'),
    operation('getProductById', 'get', '/products/{productId}'),
    operation('deleteProduct', 'delete', '/products/{productId}'),
    operation('createProductWithOwner', 'post', '/products/with-owner', tags=()),
    operation('adminListProducts', 'get', '/admin/products', tags=()),
)
