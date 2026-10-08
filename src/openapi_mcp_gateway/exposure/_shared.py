import dataclasses
import functools
import hashlib
import keyword
import operator
import re
import typing

import inflection
import pydantic
import typing_extensions

from ..openapi import (
    DEFS_REF_PREFIX,
    OperationInfo,
    ParameterInfo,
    _deep_merge,
    count_defs_references,
    defs_reference,
    map_subschemas,
    schema_description,
)


_INVALID_IDENTIFIER_CHARS = re.compile(r'[^A-Za-z0-9_]')
# The longest ``$defs`` key the strictest client accepts, Anthropic's tool input schema allowing 64 characters.
# A longer component name keeps a readable head and gains a digest of the whole name, so it stays distinct.
_DEFS_KEY_LIMIT = 64
_DEFS_KEY_DIGEST_LENGTH = 8
# Keywords that say something about a value without changing which Python type it parses into,
# so a reference carrying only these still maps to the component's shared type.
_ANNOTATION_KEYWORDS = frozenset(
    {'description', 'title', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly', '$comment'}
)


def _sanitize_name(name: str) -> str:
    """Coerce ``name`` to a valid Python identifier (digit prefix, keyword suffix)."""
    sanitized_name = _INVALID_IDENTIFIER_CHARS.sub('_', name)
    if sanitized_name and sanitized_name[0].isdigit():
        sanitized_name = '_' + sanitized_name
    if keyword.iskeyword(sanitized_name):
        sanitized_name += '_'
    return sanitized_name


def _claim_name(candidate: str, taken: set[str], limit: int | None = None) -> str:
    """Return ``candidate``, or it with the lowest numeric suffix not in ``taken``, and add the result to ``taken``.

    ``limit`` caps the length, trimming ``candidate`` to make room for the suffix.
    Checking against the names actually handed out, rather than deriving them collision-free,
    holds for any spec, since the transforms that make a name readable are lossy.
    """
    name = candidate
    counter = 1
    while name in taken:
        counter += 1
        suffix = str(counter)
        name = (candidate if limit is None else candidate[: limit - len(suffix)]) + suffix
    taken.add(name)
    return name


@dataclasses.dataclass
class _ComponentTypes:
    """The generated types of one signature: one per ``$defs`` entry, and a distinct class name for every model.

    Every path reaching an entry shares its one type.
    ``prefix`` namespaces the generated model names by operation, so two tools reusing a component stay distinct.
    Class names are claimed from one registry, so no two models in a signature share one, whatever the spec's names.
    """

    defs: dict[str, dict[str, typing.Any]]
    prefix: str
    _built: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    # The class name claimed for each entry still being built, which a reference back into it is a forward reference to.
    _building: dict[str, str] = dataclasses.field(default_factory=dict)
    # The entries a reference reached while they were still being built.
    _recursive: set[str] = dataclasses.field(default_factory=set)
    # What each forward reference resolves to.
    _namespace: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    _names: set[str] = dataclasses.field(default_factory=set)
    _models: list[type[pydantic.BaseModel]] = dataclasses.field(default_factory=list)

    def claim(self, name_hint: str) -> str:
        """Return a class name for a new model, ``name_hint`` itself unless another model already has it."""
        return _claim_name(name_hint, self._names)

    def create_model(self, class_name: str, fields: dict[str, typing.Any]) -> type[pydantic.BaseModel]:
        """Create a model under an already claimed ``class_name``."""
        model = pydantic.create_model(class_name, **fields)
        self._models.append(model)
        return model

    def resolve(self, name: str) -> typing.Any:
        """Return the Python type for the entry ``name``, building it on first use.

        A reference back into an entry still being built is a recursive component,
        and maps to a forward reference resolved once the outermost entry is built,
        so every level of a recursive value is parsed the same way.
        A recursive entry whose type is not itself a model, such as a union, becomes a type alias to refer to.
        """
        if name in self._built:
            return self._built[name]
        if name in self._building:
            self._recursive.add(name)
            return typing.ForwardRef(self._building[name])
        class_name = self.claim(f'{self.prefix}{inflection.camelize(_sanitize_name(name))}')
        self._building[name] = class_name
        try:
            python_type = _schema_to_python_type(
                self.defs.get(name, {}), name_hint=class_name, components=self, name_claimed=True
            )
        finally:
            del self._building[name]
        if name in self._recursive and getattr(python_type, '__name__', None) != class_name:
            # Built at runtime from the spec, which a static checker expects to see declared as a module-level alias.
            python_type = typing_extensions.TypeAliasType(class_name, python_type)  # pyright: ignore[reportGeneralTypeIssues]
        self._namespace[class_name] = python_type
        self._built[name] = python_type
        if not self._building:
            for model in self._models:
                if not model.__pydantic_complete__:
                    model.model_rebuild(_types_namespace=self._namespace)
        return python_type


def _schema_to_python_type(
    schema: dict[str, typing.Any],
    *,
    name_hint: str = 'NestedObject',
    components: _ComponentTypes | None = None,
    name_claimed: bool = False,
) -> typing.Any:
    """Map a JSON Schema fragment to a Python type annotation.

    A ``#/$defs/<Name>`` reference resolves through ``components`` to that entry's one shared type.
    One carrying keywords that reshape the value, such as ``properties`` beside it, gets a type of its own instead,
    built from the entry merged with those keywords.
    Resolves ``oneOf`` / ``anyOf`` next, since union fragments often omit ``type``.
    An ``enum`` fragment becomes a ``Literal`` so the allowed values appear inline in the LLM-facing schema.
    A ``type: object`` fragment with ``properties`` becomes a dynamic pydantic model,
    so its nested fields, their descriptions, and required-ness survive into the JSON Schema for the LLM.
    Without that step the object would collapse to ``dict[str, typing.Any]`` and the LLM would guess every field.
    A fragment with neither a recognised ``type`` nor a union resolves to ``typing.Any``.

    ``name_hint`` becomes the generated model's class name, with a numeric suffix if another model has it,
    unless ``name_claimed`` says the caller already claimed it.
    Callers should namespace it by operation and property, so the names stay readable.
    """
    if components is None:
        components = _ComponentTypes({}, prefix='')

    defs_name = defs_reference(schema)
    if defs_name is not None:
        siblings = {key: value for key, value in schema.items() if key != '$ref'}
        if siblings.keys() <= _ANNOTATION_KEYWORDS:
            return components.resolve(defs_name)
        return _schema_to_python_type(
            _deep_merge(components.defs.get(defs_name, {}), siblings),
            name_hint=name_hint,
            components=components,
            name_claimed=name_claimed,
        )

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
            else _schema_to_python_type(
                {**schema, 'type': member}, name_hint=name_hint, components=components, name_claimed=name_claimed
            )
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
                name_hint=f'{name_hint}{inflection.camelize(_sanitize_name(property_name))}',
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
        class_name = name_hint if name_claimed else components.claim(name_hint)
        return components.create_model(class_name, model_fields)
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


def _defs_keys(names: typing.Iterable[str]) -> dict[str, str]:
    """Map each entry name to the ``$defs`` key it is advertised under, distinct and at most 64 characters long.

    A name within the limit is its own key, since the spec's name is the most informative label.
    """
    keys: dict[str, str] = {}
    taken: set[str] = set()
    for name in sorted(names, key=lambda name: (len(name) > _DEFS_KEY_LIMIT, name)):
        candidate = name
        if len(name) > _DEFS_KEY_LIMIT:
            digest = hashlib.sha256(name.encode()).hexdigest()[:_DEFS_KEY_DIGEST_LENGTH]
            candidate = f'{name[: _DEFS_KEY_LIMIT - _DEFS_KEY_DIGEST_LENGTH - 1]}_{digest}'
        keys[name] = _claim_name(candidate, taken, limit=_DEFS_KEY_LIMIT)
    return keys


def _inline_single_use(
    schema: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
    shared_keys: dict[str, str],
) -> dict[str, typing.Any]:
    """Replace each reference to an entry outside ``shared_keys`` with the entry itself, at any depth.

    A reference to a shared entry is pointed at its advertised key.
    Keywords beside an inlined reference, such as a parameter's own ``description`` or a shaped ``default``,
    are merged into the entry and win over it.
    This terminates on a recursive component, since a cycle is always reached at least twice
    (once from outside it and once from within), so one of its entries is always shared.
    """

    def inline(subschema: dict[str, typing.Any]) -> dict[str, typing.Any]:
        return _inline_single_use(subschema, defs, shared_keys)

    name = defs_reference(schema)
    if name is None or name not in defs:
        return map_subschemas(schema, inline)
    siblings = map_subschemas({key: value for key, value in schema.items() if key != '$ref'}, inline)
    if name in shared_keys:
        return {'$ref': DEFS_REF_PREFIX + shared_keys[name], **siblings}
    return _deep_merge(inline(defs[name]), siblings)


def _with_shared_defs(schema: dict[str, typing.Any], defs: dict[str, dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """Advertise each component ``schema`` reaches more than once as one ``$defs`` entry, inlining the rest.

    A component reached through several paths is described once and referenced from each,
    rather than repeating its field descriptions per path.
    A component reached once reads better inline and costs nothing more there,
    so a tool with no repetition advertises exactly the fully inlined schema.
    A recursive component is always reached more than once, so it lands in ``$defs`` and is expressed in full.
    Entry keys are the spec's component names, which cannot collide since each tool's schema is its own document,
    except that a name past what clients accept is shortened, see ``_defs_keys``.
    """
    counts = count_defs_references([schema], defs)
    shared_keys = _defs_keys(name for name, count in counts.items() if count > 1)
    result = _inline_single_use(schema, defs, shared_keys)
    if shared_keys:
        result['$defs'] = {
            shared_keys[name]: _inline_single_use(defs[name], defs, shared_keys) for name in sorted(shared_keys)
        }
    return result


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
        if parameter.description and not schema_description(property_schema, operation.schema_defs):
            property_schema['description'] = parameter.description
        properties[parameter_name] = property_schema
        if parameter.required:
            required_property_names.append(parameter_name)
    schema: dict[str, typing.Any] = {'type': 'object', 'properties': properties}
    if required_property_names:
        schema['required'] = required_property_names
    return _with_shared_defs(schema, operation.schema_defs)
