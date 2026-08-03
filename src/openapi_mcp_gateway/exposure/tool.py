import dataclasses
import inspect
import logging
import typing

import inflection
import pydantic
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations

from ..openapi import OperationInfo
from ._shared import (
    UpstreamBinding,
    _get_override,
    _iter_unique_sanitised_parameters,
    _schema_to_python_type,
    build_input_schema,
    derive_description,
    derive_name,
)
from ._upstream import _build_success_result, _build_upstream_closure


logger = logging.getLogger(__name__)


_LIST_OPERATIONS_DESCRIPTION = (
    'List every operation available on this server. '
    'Returns `{operations: [{name, description}, ...]}`. '
    'Call `get_operation(name)` next to fetch the input schema for one specific operation.'
)

_GET_OPERATION_DESCRIPTION = (
    'Return the input schema for one operation. '
    'Returns `{name, description, input_schema}`. '
    'Use the JSON Schema to construct the `arguments` payload for `call_operation`.'
)

_CALL_OPERATION_DESCRIPTION = (
    'Invoke an operation by name with a JSON object of arguments. '
    'Argument keys must match the keys in `get_operation(name).input_schema.properties`.'
)


def derive_tool_title(operation: OperationInfo) -> str | None:
    """Return the MCP tool title for ``operation``, or ``None`` to omit the field.

    Uses OpenAPI ``summary`` when present.
    """
    return operation.summary or None


def derive_tool_annotations(operation: OperationInfo) -> ToolAnnotations:
    """Derive MCP ``ToolAnnotations`` for ``operation`` from its HTTP method.

    ``GET`` is read-only and idempotent, ``PUT`` / ``PATCH`` / ``DELETE`` are idempotent,
    ``DELETE`` is additionally destructive, and every tool is open-world.
    ``title`` mirrors ``Tool.title`` for clients still reading the legacy annotations field.
    """
    method = operation.method.lower()
    return ToolAnnotations(
        title=operation.summary or None,
        read_only_hint=(method == 'get') or None,
        destructive_hint=(method == 'delete') or None,
        idempotent_hint=(method in {'get', 'put', 'patch', 'delete'}) or None,
        open_world_hint=True,
    )


def _build_tool_signature(operation: OperationInfo) -> tuple[inspect.Signature, dict[str, typing.Any]]:
    """Build the ``inspect.Signature`` and annotations MCPServer reads to derive the tool input schema.

    Parameter order is required parameters first, then the framework-injected ``ctx``,
    then optional parameters with ``default=None``.
    Dedupes by sanitised identifier.
    """
    ordered_parameters = list(_iter_unique_sanitised_parameters(operation.parameters))
    # Namespace nested model names by operationId, so two tools that expose a same-named body param
    # get distinct generated classes in the resulting JSON Schema.
    operation_name_prefix = inflection.camelize(operation.operation_id)

    annotations: dict[str, typing.Any] = {}
    signature_parameters: list[inspect.Parameter] = []

    for parameter_name, parameter in ordered_parameters:
        if not parameter.required:
            continue
        python_type = _schema_to_python_type(
            parameter.schema_, name_hint=f'{operation_name_prefix}{inflection.camelize(parameter_name)}'
        )
        annotation = (
            typing.Annotated[python_type, pydantic.Field(description=parameter.description)]
            if parameter.description
            else python_type
        )
        signature_parameters.append(
            inspect.Parameter(
                name=parameter_name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=annotation,
            )
        )
        annotations[parameter_name] = annotation

    signature_parameters.append(
        inspect.Parameter(name='ctx', kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    )
    annotations['ctx'] = Context

    for parameter_name, parameter in ordered_parameters:
        if parameter.required:
            continue
        python_type = _schema_to_python_type(
            parameter.schema_, name_hint=f'{operation_name_prefix}{inflection.camelize(parameter_name)}'
        )
        annotation = (
            typing.Annotated[python_type | None, pydantic.Field(description=parameter.description)]
            if parameter.description
            else python_type | None
        )
        signature_parameters.append(
            inspect.Parameter(
                name=parameter_name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=annotation,
                default=None,
            )
        )
        annotations[parameter_name] = annotation

    return inspect.Signature(signature_parameters), annotations


def _unknown_operation_error(name: str) -> ValueError:
    """Build the standard error raised by ``get_operation`` and ``call_operation`` when ``name`` is not indexed."""
    return ValueError(f'Unknown operation: {name!r}. Use list_operations to see available names.')


def build_tool_function(
    operation: OperationInfo,
    binding: UpstreamBinding,
    *,
    attach_signature: bool = True,
) -> typing.Callable:
    """Build the async callable for ``operation``.

    With ``attach_signature=True`` (the default),
    the returned callable carries an ``inspect.Signature`` and ``__annotations__`` so MCPServer can derive its input schema.
    With ``attach_signature=False``, returns the bare upstream closure for dispatch from a registry,
    as used by :class:`MetaToolGenerator`.
    """
    upstream_callable = _build_upstream_closure(operation, binding, validate_input=True)
    if not attach_signature:
        return upstream_callable
    signature, annotations = _build_tool_signature(operation)
    upstream_callable.__signature__ = signature
    upstream_callable.__annotations__ = annotations
    return upstream_callable


@dataclasses.dataclass
class _MetaToolEntry:
    """One operation indexed by the dynamic-exposure registry.

    Internal to :class:`MetaToolGenerator`,
    consumers interact with operations through the three meta-tools, not this dataclass.
    """

    description: str
    input_schema: dict[str, typing.Any]
    callable_: typing.Callable[..., typing.Awaitable[CallToolResult]]


class ToolGenerator:
    """Static exposure: register one MCP tool per operation onto ``mcp``."""

    def __init__(self, mcp: MCPServer, binding: UpstreamBinding):
        self.mcp = mcp
        self.binding = binding

    def register(self, operations: list[OperationInfo]) -> None:
        """Declare one MCP tool per ``OperationInfo`` in ``operations``."""
        for operation in operations:
            override = _get_override(operation, 'tool')
            tool_function = build_tool_function(operation, self.binding)
            name = derive_name(operation, override.name if override else None)
            description = derive_description(operation, override.description if override else None)
            title = derive_tool_title(operation)
            annotations = derive_tool_annotations(operation)
            registered = self.mcp._tool_manager.add_tool(
                tool_function,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
            )
            # The high-level registration derives the advertised input schema from the Python signature,
            # which cannot carry OpenAPI keywords such as format, numeric bounds, pattern, enum, or composition.
            # Overwrite it with the schema built straight from the operation, so the LLM sees the real contract.
            # build_tool_function enforces this same schema before the upstream call, so display and validation match.
            registered.parameters = build_input_schema(operation)
            logger.debug('Tool registered: %s ← %s %s', name, operation.method.upper(), operation.path)
        logger.info('Registered %d MCP tool(s) on server "%s"', len(operations), self.mcp.name)


class MetaToolGenerator:
    """Dynamic exposure: register ``list_operations`` / ``get_operation`` / ``call_operation`` meta-tools.

    The LLM walks list → get → call to discover and invoke a specific operation.
    Per-operation callables come from the same upstream closure the static path uses,
    so auth, path substitution, and request shape stay identical across modes.
    """

    def __init__(self, mcp: MCPServer, binding: UpstreamBinding):
        self.mcp = mcp
        self.binding = binding
        self._registry: dict[str, _MetaToolEntry] = {}

    def register(self, operations: list[OperationInfo]) -> None:
        """Populate the registry from ``operations`` and bind the three meta-tools."""
        for operation in operations:
            override = _get_override(operation, 'tool')
            name = derive_name(operation, override.name if override else None)
            self._registry[name] = _MetaToolEntry(
                description=derive_description(operation, override.description if override else None),
                input_schema=build_input_schema(operation),
                callable_=build_tool_function(operation, self.binding, attach_signature=False),
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
        async def list_operations(ctx: Context) -> CallToolResult:
            entries = [{'name': name, 'description': entry.description} for name, entry in registry.items()]
            return _build_success_result({'operations': entries})

        @self.mcp.tool(name='get_operation', description=_GET_OPERATION_DESCRIPTION)
        async def get_operation(name: str, ctx: Context) -> CallToolResult:
            entry = registry.get(name)
            if entry is None:
                raise _unknown_operation_error(name)
            return _build_success_result(
                {'name': name, 'description': entry.description, 'input_schema': entry.input_schema}
            )

        @self.mcp.tool(name='call_operation', description=_CALL_OPERATION_DESCRIPTION)
        async def call_operation(name: str, arguments: dict[str, typing.Any], ctx: Context) -> CallToolResult:
            entry = registry.get(name)
            if entry is None:
                raise _unknown_operation_error(name)
            return await entry.callable_(**arguments, ctx=ctx)
