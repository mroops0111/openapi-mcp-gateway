import inspect
import logging
import typing

import inflection
import pydantic
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from ..openapi import ExposedTool, OperationInfo, ToolOverride
from ._shaping import shape_operation
from ._shared import (
    _get_override,
    _iter_unique_sanitised_parameters,
    _schema_to_python_type,
    _SignatureTypes,
    build_input_schema,
    derive_description,
    derive_name,
)
from ._upstream import UpstreamBinding, _build_upstream_closure


logger = logging.getLogger(__name__)


def _shaping(tool_override: ToolOverride | None) -> dict[str, typing.Any] | None:
    """Report which reshaping took effect, or ``None`` when the call passes through untouched."""
    if tool_override is None:
        return None
    shaping: dict[str, typing.Any] = {
        'params': tool_override.params_strategy if tool_override.params and tool_override.params_strategy else None,
        'request': bool(tool_override.request),
        'response': bool(tool_override.response),
        'media': bool(tool_override.media),
    }
    return shaping if any(shaping.values()) else None


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


def merge_tool_annotations(
    operation: OperationInfo,
    override_annotations: dict[str, typing.Any] | None,
) -> ToolAnnotations:
    """Merge author ``annotations`` overrides over the method-derived ones, explicit values winning.

    Dropping ``None`` on both sides lets an author flip a single hint,
    for example marking a POST read-only, while inheriting every hint they did not set.
    Override keys may be camelCase (``readOnlyHint``) or snake_case (``read_only_hint``).
    """
    derived = derive_tool_annotations(operation).model_dump(exclude_none=True)
    if not override_annotations:
        return ToolAnnotations.model_validate(derived)
    override = ToolAnnotations.model_validate(override_annotations).model_dump(exclude_none=True)
    return ToolAnnotations.model_validate({**derived, **override})


def _build_tool_signature(operation: OperationInfo) -> tuple[inspect.Signature, dict[str, typing.Any]]:
    """Build the ``inspect.Signature`` and annotations MCPServer reads to derive the tool input schema.

    Parameter order is required parameters first, then the framework-injected ``ctx``,
    then optional parameters with ``default=None``.
    Dedupes by sanitised identifier.
    """
    ordered_parameters = [
        (name, parameter)
        for name, parameter in _iter_unique_sanitised_parameters(operation.parameters)
        if parameter.visible
    ]
    # Namespace nested model names by operationId,
    # so two tools that expose a same-named body param get distinct generated classes in the resulting JSON Schema.
    operation_name_prefix = inflection.camelize(operation.operation_id)
    # One model per component for the whole tool, however many parameters or paths reach it.
    signature_types = _SignatureTypes(operation.schema_defs, operation_name_prefix)

    annotations: dict[str, typing.Any] = {}
    signature_parameters: list[inspect.Parameter] = []

    for parameter_name, parameter in ordered_parameters:
        if not parameter.required:
            continue
        python_type = _schema_to_python_type(
            parameter.schema_,
            name_hint=f'{operation_name_prefix}{inflection.camelize(parameter_name)}',
            signature_types=signature_types,
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
            parameter.schema_,
            name_hint=f'{operation_name_prefix}{inflection.camelize(parameter_name)}',
            signature_types=signature_types,
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


def build_tool_function(
    operation: OperationInfo,
    binding: UpstreamBinding,
    *,
    input_schema: dict[str, typing.Any] | None = None,
    attach_signature: bool = True,
) -> typing.Callable:
    """Build the async callable for ``operation``.

    The callable checks its arguments against ``input_schema`` before any upstream call.
    Pass the schema the tool advertises, so the two cannot drift apart.
    Without it, the schema is built from ``operation`` with ``build_input_schema``.

    With ``attach_signature=True`` (the default),
    the returned callable carries an ``inspect.Signature`` and ``__annotations__``,
    so MCPServer can derive its input schema.
    With ``attach_signature=False``, returns the bare upstream closure for dispatch from a registry,
    as used by :class:`MetaToolGenerator`.
    """
    if input_schema is None:
        input_schema = build_input_schema(operation)
    upstream_callable = _build_upstream_closure(operation, binding, input_schema=input_schema)
    if not attach_signature:
        return upstream_callable
    signature, annotations = _build_tool_signature(operation)
    upstream_callable.__signature__ = signature
    upstream_callable.__annotations__ = annotations
    return upstream_callable


class ToolGenerator:
    """Register one MCP tool per operation onto ``mcp``, for static exposure."""

    def __init__(self, mcp: MCPServer, binding: UpstreamBinding):
        self.mcp = mcp
        self.binding = binding

    def register(self, operations: list[OperationInfo]) -> list[ExposedTool]:
        """Declare one MCP tool per ``OperationInfo`` in ``operations``, returning what was exposed."""
        exposed_tools: list[ExposedTool] = []
        for operation in operations:
            # Shape once, then feed the shaped operation to every consumer (schema, signature, upstream),
            # so hidden / default / description overrides stay consistent across all of them.
            shaped_operation = shape_operation(operation)
            # The input schema is the tool's contract, built once and both advertised and enforced.
            input_schema = build_input_schema(shaped_operation)
            tool_override = _get_override(shaped_operation, 'tool')
            tool_function = build_tool_function(shaped_operation, self.binding, input_schema=input_schema)
            name = derive_name(shaped_operation, tool_override.name if tool_override else None)
            description = derive_description(shaped_operation, tool_override.description if tool_override else None)
            title = derive_tool_title(shaped_operation)
            annotations = merge_tool_annotations(shaped_operation, tool_override.annotations if tool_override else None)
            registered = self.mcp._tool_manager.add_tool(
                tool_function,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
            )
            # The high-level registration derives the advertised input schema from the Python signature,
            # which cannot carry OpenAPI keywords such as format, numeric bounds, pattern, enum, or composition.
            # Overwrite it with the contract built above, the same schema the tool function enforces.
            registered.parameters = input_schema
            exposed_tools.append(
                ExposedTool(
                    name,
                    shaped_operation.method,
                    shaped_operation.path,
                    _shaping(tool_override),
                    description=description,
                    input_schema=input_schema,
                )
            )
            logger.debug('Tool registered: %s ← %s %s', name, shaped_operation.method.upper(), shaped_operation.path)
        logger.info('Registered %d MCP tool(s) on server "%s"', len(operations), self.mcp.name)
        return exposed_tools
