import functools
import keyword
import operator
import re
import typing

import inflection
import pydantic

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


def _schema_to_python_type(
    schema: dict[str, typing.Any],
    *,
    name_hint: str = 'NestedObject',
) -> typing.Any:
    """Map a JSON Schema fragment to a Python type annotation.

    Resolves ``oneOf`` / ``anyOf`` first, since union fragments often omit ``type``.
    An ``enum`` fragment becomes a ``Literal`` so the allowed values appear inline in the LLM-facing schema.
    A ``type: object`` fragment with ``properties`` becomes a dynamic pydantic model,
    so its nested fields, their descriptions, and required-ness survive into the JSON Schema for the LLM.
    Without that step the object would collapse to ``dict[str, typing.Any]`` and the LLM would guess every field.
    A fragment with neither a recognised ``type`` nor a union resolves to ``typing.Any``.

    ``name_hint`` becomes the generated model's class name.
    Callers should namespace it by operation and property,
    so models from different tools do not collide in the resulting JSON Schema ``$defs`` section.
    """
    variants = schema.get('oneOf') or schema.get('anyOf')
    if variants:
        types = [
            _schema_to_python_type(variant, name_hint=f'{name_hint}Variant{index}')
            for index, variant in enumerate(variants)
        ]
        if len(types) == 1:
            return types[0]
        return functools.reduce(operator.or_, types)

    enum_values = schema.get('enum')
    if enum_values:
        return typing.Literal[tuple(enum_values)]

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
        item_type = _schema_to_python_type(items, name_hint=f'{name_hint}Item')
        return list[item_type]
    if schema_type == 'object':
        properties = schema.get('properties')
        if not properties:
            return dict[str, typing.Any]
        required_property_names = set(schema.get('required', []))
        model_fields: dict[str, typing.Any] = {}
        for property_name, property_schema in properties.items():
            property_type = _schema_to_python_type(
                property_schema,
                name_hint=f'{name_hint}{inflection.camelize(property_name)}',
            )
            field_kwargs: dict[str, typing.Any] = {}
            property_description = property_schema.get('description')
            if property_description:
                field_kwargs['description'] = property_description
            if property_name in required_property_names:
                model_fields[property_name] = (property_type, pydantic.Field(**field_kwargs))
            else:
                model_fields[property_name] = (property_type | None, pydantic.Field(default=None, **field_kwargs))
        return pydantic.create_model(name_hint, **model_fields)
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
    """Yield ``(sanitised_name, parameter)`` for each parameter,
    skipping repeats of the same sanitised name.
    """
    seen_parameter_names: set[str] = set()
    for parameter in parameters:
        parameter_name = _sanitize_name(parameter.name)
        if parameter_name in seen_parameter_names:
            continue
        seen_parameter_names.add(parameter_name)
        yield parameter_name, parameter


def _get_override(operation: OperationInfo, kind: typing.Literal['tool', 'resource']) -> typing.Any:
    """Return ``operation.x_mcp_integration.<kind>`` or ``None`` when no override exists."""
    return getattr(operation.x_mcp_integration, kind)


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

    Dedupes properties by sanitised name and only emits ``required`` when at least one parameter is required.
    Used by the dynamic tool exposure to advertise per-operation input shapes through the ``get_operation`` meta-tool.
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
