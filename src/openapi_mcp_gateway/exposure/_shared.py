import collections
import dataclasses
import functools
import keyword
import operator
import re
import typing

import inflection
import pydantic

from ..openapi import OperationInfo, ParameterInfo, defs_reference, iter_subschemas, map_subschemas


_INVALID_IDENTIFIER_CHARS = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_name(name: str) -> str:
    """Coerce ``name`` to a valid Python identifier (digit prefix, keyword suffix)."""
    sanitized_name = _INVALID_IDENTIFIER_CHARS.sub('_', name)
    if sanitized_name and sanitized_name[0].isdigit():
        sanitized_name = '_' + sanitized_name
    if keyword.iskeyword(sanitized_name):
        sanitized_name += '_'
    return sanitized_name


@dataclasses.dataclass
class _ComponentTypes:
    """The Python type for each ``$defs`` entry of one operation, built once and shared by every path reaching it.

    ``prefix`` namespaces the generated model names by operation, so two tools reusing a component stay distinct.
    """

    defs: dict[str, dict[str, typing.Any]]
    prefix: str
    _built: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    _building: set[str] = dataclasses.field(default_factory=set)

    def resolve(self, name: str) -> typing.Any:
        """Return the Python type for the entry ``name``, building it on first use.

        A reference back into an entry still being built is a recursive component.
        It maps to ``typing.Any``, since a dynamic model cannot refer to itself before it exists,
        and the advertised schema, enforced on every call, still describes the full shape.
        """
        if name in self._built:
            return self._built[name]
        if name in self._building:
            return typing.Any
        self._building.add(name)
        python_type = _schema_to_python_type(
            self.defs.get(name, {}),
            name_hint=f'{self.prefix}{inflection.camelize(_sanitize_name(name))}',
            components=self,
        )
        self._building.discard(name)
        self._built[name] = python_type
        return python_type


def _schema_to_python_type(
    schema: dict[str, typing.Any],
    *,
    name_hint: str = 'NestedObject',
    components: _ComponentTypes | None = None,
) -> typing.Any:
    """Map a JSON Schema fragment to a Python type annotation.

    A ``#/$defs/<Name>`` reference resolves through ``components`` to that entry's one shared type,
    or to ``typing.Any`` when no ``components`` is given.
    Resolves ``oneOf`` / ``anyOf`` next, since union fragments often omit ``type``.
    An ``enum`` fragment becomes a ``Literal`` so the allowed values appear inline in the LLM-facing schema.
    A ``type: object`` fragment with ``properties`` becomes a dynamic pydantic model,
    so its nested fields, their descriptions, and required-ness survive into the JSON Schema for the LLM.
    Without that step the object would collapse to ``dict[str, typing.Any]`` and the LLM would guess every field.
    A fragment with neither a recognised ``type`` nor a union resolves to ``typing.Any``.

    ``name_hint`` becomes the generated model's class name.
    Callers should namespace it by operation and property,
    so models from different tools do not collide in the resulting JSON Schema ``$defs`` section.
    """
    defs_name = defs_reference(schema)
    if defs_name is not None:
        return components.resolve(defs_name) if components is not None else typing.Any

    variants = schema.get('oneOf') or schema.get('anyOf')
    if variants:
        types = [
            _schema_to_python_type(variant, name_hint=f'{name_hint}Variant{index}', components=components)
            for index, variant in enumerate(variants)
        ]
        if len(types) == 1:
            return types[0]
        return functools.reduce(operator.or_, types)

    enum_values = schema.get('enum')
    if enum_values:
        return typing.Literal[tuple(enum_values)]

    schema_type = schema.get('type')
    if isinstance(schema_type, list):
        member_types = [
            type(None)
            if member == 'null'
            else _schema_to_python_type({**schema, 'type': member}, name_hint=name_hint, components=components)
            for member in schema_type
        ]
        deduped: list[typing.Any] = []
        for member_type in member_types:
            if member_type not in deduped:
                deduped.append(member_type)
        return deduped[0] if len(deduped) == 1 else functools.reduce(operator.or_, deduped)
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
        item_type = _schema_to_python_type(items, name_hint=f'{name_hint}Item', components=components)
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
                components=components,
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


def _count_defs_references(
    schema: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
) -> collections.Counter[str]:
    """Count the places in ``schema`` that reach each entry of ``defs``.

    An entry's own references are counted once, however often the entry is reached,
    since the entry is written out once, whether under ``$defs`` or inlined at its only use.
    """
    counts: collections.Counter[str] = collections.Counter()
    pending = [schema]
    while pending:
        node = pending.pop()
        name = defs_reference(node)
        if name is not None and name in defs:
            if name not in counts:
                pending.append(defs[name])
            counts[name] += 1
        pending.extend(iter_subschemas(node))
    return counts


def _inline_single_use(
    schema: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
    shared: set[str],
) -> dict[str, typing.Any]:
    """Replace each reference to an entry outside ``shared`` with the entry itself, at any depth.

    Keywords beside the reference, such as a parameter's own ``description`` or a shaped ``default``,
    win over the entry's.
    This terminates on a recursive component, since a cycle is always reached at least twice
    (once from outside it and once from within), so one of its entries is always ``shared``.
    """
    name = defs_reference(schema)
    if name is not None and name in defs and name not in shared:
        siblings = {key: value for key, value in schema.items() if key != '$ref'}
        return {**_inline_single_use(defs[name], defs, shared), **siblings}
    return map_subschemas(schema, lambda subschema: _inline_single_use(subschema, defs, shared))


def _with_shared_defs(schema: dict[str, typing.Any], defs: dict[str, dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """Advertise each component ``schema`` reaches more than once as one ``$defs`` entry, inlining the rest.

    A component reached through several paths is described once and referenced from each,
    rather than repeating its field descriptions per path.
    A component reached once reads better inline and costs nothing more there,
    so a tool with no repetition advertises exactly the fully inlined schema.
    A recursive component is always reached more than once, so it lands in ``$defs`` and is expressed in full.
    Entry names are the spec's component names, which cannot collide since each tool's schema is its own document.
    """
    counts = _count_defs_references(schema, defs)
    shared = {name for name, count in counts.items() if count > 1}
    result = _inline_single_use(schema, defs, shared)
    if shared:
        result['$defs'] = {name: _inline_single_use(defs[name], defs, shared) for name in sorted(shared)}
    return result


def _declares_description(schema: dict[str, typing.Any], defs: dict[str, dict[str, typing.Any]]) -> bool:
    """Report whether ``schema`` carries a ``description``, looking through a reference at its root."""
    name = defs_reference(schema)
    return 'description' in schema or (name is not None and 'description' in defs.get(name, {}))


def build_input_schema(operation: OperationInfo) -> dict[str, typing.Any]:
    """Build the JSON Schema describing ``operation`` inputs, the one a tool advertises and enforces.

    Dedupes properties by sanitised name and only emits ``required`` when at least one parameter is required.
    A component reached more than once is carried under ``$defs``, see ``_with_shared_defs``.
    Used by the static tool exposure as the advertised ``inputSchema``,
    and by the dynamic tool exposure to advertise per-operation input shapes through the ``get_operation`` meta-tool.
    """
    properties: dict[str, typing.Any] = {}
    required_property_names: list[str] = []
    for parameter_name, parameter in _iter_unique_sanitised_parameters(operation.parameters):
        if not parameter.visible:
            continue
        property_schema = dict(parameter.schema_) if parameter.schema_ else {'type': 'string'}
        if parameter.description and not _declares_description(property_schema, operation.schema_defs):
            property_schema['description'] = parameter.description
        properties[parameter_name] = property_schema
        if parameter.required:
            required_property_names.append(parameter_name)
    schema: dict[str, typing.Any] = {'type': 'object', 'properties': properties}
    if required_property_names:
        schema['required'] = required_property_names
    return _with_shared_defs(schema, operation.schema_defs)
