"""Shared internals used by every exposure strategy.

Identifier sanitising, JSON Schema to Python type mapping, parameter bucketing,
and the override-aware ``derive_name`` / ``derive_description`` helpers all live here.
"""

import dataclasses
import functools
import keyword
import operator
import re
import typing

import httpx
import inflection

from ..auth.resolver import AuthResolver, NullAuthResolver
from ..openapi import OperationInfo, ParameterInfo


_INVALID_IDENTIFIER_CHARS = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_name(name: str) -> str:
    """Coerce ``name`` to a valid Python identifier (digit prefix, keyword suffix)."""
    sanitized_name = _INVALID_IDENTIFIER_CHARS.sub('_', name)
    if sanitized_name and sanitized_name[0].isdigit():
        sanitized_name = '_' + sanitized_name
    if keyword.iskeyword(sanitized_name):
        sanitized_name += '_'
    return sanitized_name


def _schema_to_python_type(schema: dict[str, typing.Any]) -> typing.Any:
    """Map a JSON Schema fragment to a Python type annotation.

    Resolves ``oneOf`` / ``anyOf`` first since union fragments often omit ``type``.
    A fragment with neither resolves to ``typing.Any``.
    """
    variants = schema.get('oneOf') or schema.get('anyOf')
    if variants:
        types = [_schema_to_python_type(variant) for variant in variants]
        if len(types) == 1:
            return types[0]
        return functools.reduce(operator.or_, types)

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
        [parameter for parameter in parameters if parameter.location == 'path'],
        [parameter for parameter in parameters if parameter.location == 'query'],
        [parameter for parameter in parameters if parameter.location == 'header'],
        [parameter for parameter in parameters if parameter.location == 'body'],
    )


def _iter_unique_sanitised_parameters(
    parameters: typing.Iterable[ParameterInfo],
) -> typing.Iterator[tuple[str, ParameterInfo]]:
    """Yield ``(sanitised_name, parameter)`` for each parameter, skipping repeats of the same sanitised name."""
    seen_parameter_names: set[str] = set()
    for parameter in parameters:
        parameter_name = _sanitize_name(parameter.name)
        if parameter_name in seen_parameter_names:
            continue
        seen_parameter_names.add(parameter_name)
        yield parameter_name, parameter


def _get_override(operation: OperationInfo, kind: typing.Literal['tool', 'resource']) -> typing.Any:
    """Return ``operation.x_mcp_integration.expose.<kind>`` or ``None`` when no override exists."""
    expose = operation.x_mcp_integration.expose
    return getattr(expose, kind) if expose else None


def derive_name(operation: OperationInfo, override_name: str | None) -> str:
    """Return the MCP-side identifier for ``operation``, honoring an explicit override.

    With no override, derives the name from ``operationId`` (underscored and sanitised).
    Shared by tool, resource, and meta-tool exposure modes.
    """
    if override_name:
        return _sanitize_name(override_name)
    return _sanitize_name(inflection.underscore(operation.operation_id))


def derive_description(operation: OperationInfo, override_description: str | None) -> str:
    """Return the description for ``operation``, honoring an explicit override and falling back to spec fields.

    Fallback chain: spec ``description`` -> spec ``summary`` -> ``METHOD /path``.
    Shared by tool, resource, and meta-tool exposure modes.
    """
    if override_description:
        return override_description
    return operation.description or operation.summary or f'{operation.method.upper()} {operation.path}'


def build_input_schema(operation: OperationInfo) -> dict[str, typing.Any]:
    """Build the JSON Schema describing ``operation`` inputs.

    Dedupes properties by sanitised name and only emits ``required``
    when at least one parameter is required.
    Used by the dynamic tool exposure to advertise per-operation input shapes
    through the ``get_operation`` meta-tool.
    """
    properties: dict[str, typing.Any] = {}
    required_property_names: list[str] = []
    for parameter_name, parameter in _iter_unique_sanitised_parameters(operation.parameters):
        property_schema = dict(parameter.schema_) if parameter.schema_ else {'type': parameter.schema_type}
        if parameter.description and 'description' not in property_schema:
            property_schema['description'] = parameter.description
        properties[parameter_name] = property_schema
        if parameter.required:
            required_property_names.append(parameter_name)
    schema: dict[str, typing.Any] = {'type': 'object', 'properties': properties}
    if required_property_names:
        schema['required'] = required_property_names
    return schema


@dataclasses.dataclass(frozen=True)
class UpstreamBinding:
    """Per-server HTTP and auth context shared by every exposure strategy."""

    base_url: str
    auth_resolver: AuthResolver = dataclasses.field(default_factory=NullAuthResolver)
    timeout: float = 90
    transport: httpx.AsyncBaseTransport | None = None
