import collections
import json
import logging
import pathlib
import re
import typing
import urllib.parse

import httpx
import pydantic
import yaml


logger = logging.getLogger(__name__)


# JSON Schema keywords whose value is a single nested schema.
_SUBSCHEMA_KEYS = ('items', 'not', 'additionalProperties', 'propertyNames', 'contains', 'if', 'then', 'else')
# Keywords whose value is a list of nested schemas.
# ``allOf`` is absent here because it is flattened separately, not recursed in place.
_SUBSCHEMA_LIST_KEYS = ('oneOf', 'anyOf', 'prefixItems')
# Keywords whose value maps a name to a nested schema.
_SUBSCHEMA_MAP_KEYS = ('properties', 'patternProperties', 'dependentSchemas')

_COMPONENT_SCHEMA_PREFIX = '#/components/schemas/'
# Where an expanded fragment points for a component schema, relative to the advertised input schema's root.
DEFS_REF_PREFIX = '#/$defs/'
# The component name grammar OpenAPI allows, which needs no JSON Pointer or URI escaping.
_COMPONENT_NAME = re.compile(r'^[A-Za-z0-9._-]+$')
# The placeholder a component's entry holds while it expands, so a reference back into it stops there.
# Compared by identity, so it is never mutated.
_EXPANDING: dict[str, typing.Any] = {}


class ParameterInfo(pydantic.BaseModel):
    """One OpenAPI parameter (path, query, header, cookie, or body) with its schema.

    A component schema anywhere in ``schema_`` stays a ``{"$ref": "#/$defs/<Name>"}``,
    resolved against the owning operation's ``schema_defs``.
    """

    name: str
    location: typing.Literal['path', 'query', 'header', 'cookie', 'body']
    required: bool = False
    description: str = ''
    schema_: dict[str, typing.Any] = pydantic.Field(default_factory=dict, alias='schema')
    # Set by shape_operation from a ParamOverride default.
    # Marks a default the author wants sent upstream even when the LLM omits the parameter.
    # The LLM never sees this flag.
    send_default: bool = False
    # Set False by shape_operation for a hidden-but-defaulted parameter,
    # which is kept for upstream assembly and default injection but omitted from the schema.
    visible: bool = True

    model_config = pydantic.ConfigDict(populate_by_name=True, extra='forbid')


class ExposedTool(typing.NamedTuple):
    """One tool a server exposes, the counterpart to ``OperationInfo``.

    ``OperationInfo`` is the operation as the spec declares it. This is what the model is shown after naming,
    filtering and shaping have run, so the names here can differ from the spec's.

    ``input_schema`` is the advertised schema itself rather than a summary of it,
    because a flattened list loses the nested body properties, enums, defaults and bounds a reviewer needs most.

    ``shaping`` reports what took effect rather than what was declared,
    and is ``None`` when nothing reshapes the call. A ``params_strategy`` with no ``params`` changes nothing.
    """

    name: str
    method: str
    path: str
    shaping: dict[str, typing.Any] | None = None
    description: str = ''
    input_schema: dict[str, typing.Any] | None = None


class ParamOverride(pydantic.BaseModel):
    """One LLM-facing parameter from ``x-mcp-integration.tool.params.<name>``, keyed by the friendly name.

    Every key other than the two meta-flags is a JSON Schema keyword
    (``type``, ``enum``, ``format``, ``default``, ``description``, ``minimum`` and so on),
    describing the value exactly as it appears in the tool's advertised input schema.

    How the entry is applied depends on ``ToolOverride.params_strategy``.
    An entry carrying a ``type`` declares the parameter's schema, either replacing a matching
    spec parameter's schema or introducing a brand-new friendly parameter.
    An entry without a ``type`` tweaks a matching spec parameter through ``default`` or ``description``.

    ``required`` lifts the parameter into the schema's required list.
    ``hidden`` removes a spec parameter from the surface.
    """

    hidden: bool = False
    required: bool = False

    model_config = pydantic.ConfigDict(extra='allow')

    @property
    def schema_fragment(self) -> dict[str, typing.Any]:
        """The JSON Schema keywords declared for this parameter (everything but the meta-flags)."""
        return dict(self.__pydantic_extra__ or {})

    @property
    def declares_schema(self) -> bool:
        """True when the entry declares a friendly parameter, detected by the presence of ``type``."""
        return 'type' in self.schema_fragment


class ToolOverride(pydantic.BaseModel):
    """Spec-author overrides for the MCP tool generated from an operation.

    ``params`` shapes the LLM-facing input schema, and ``params_strategy`` says how it relates to the spec.
    With ``merge`` the entries tweak existing spec parameters and the rest stay visible.
    With ``replace`` the entries are the whole surface and every undeclared spec parameter is dropped.
    ``params_strategy`` is required whenever ``params`` is set.

    ``request`` and ``response`` are JSONata expressions that transform the values,
    ``request`` mapping the friendly arguments into the upstream request,
    and ``response`` mapping the upstream response into what the client sees.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    name: str | None = None
    description: str | None = None
    annotations: dict[str, typing.Any] | None = None
    params: dict[str, ParamOverride] = pydantic.Field(default_factory=dict)
    params_strategy: typing.Literal['merge', 'replace'] | None = None
    request: str | None = None
    response: str | None = None


class ResourceOverride(pydantic.BaseModel):
    """Spec-author overrides for the MCP resource generated from an operation.

    ``uri_template`` overrides the auto-derived URI when set,
    and must start with ``{server_name}://`` so resources stay scoped to the owning server.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    name: str | None = None
    description: str | None = None
    mime_type: str | None = None
    uri_template: str | None = None


class McpIntegration(pydantic.BaseModel):
    """Parsed ``x-mcp-integration`` operation extension.

    ``tool`` and ``resource`` each opt the operation into that MCP primitive.
    An operation may declare both.
    """

    tool: ToolOverride | None = None
    resource: ResourceOverride | None = None


class OperationInfo(pydantic.BaseModel):
    """One HTTP operation from ``paths`` with parameters, security, and MCP integration flags."""

    operation_id: str
    method: str
    path: str
    summary: str = ''
    description: str = ''
    tags: list[str] = pydantic.Field(default_factory=list)
    parameters: list[ParameterInfo] = pydantic.Field(default_factory=list)
    security: list[dict[str, list[str]]] = pydantic.Field(default_factory=list)
    x_mcp_integration: McpIntegration = pydantic.Field(default_factory=McpIntegration)
    # The expanded component schemas the parameters reach, keyed by component name,
    # for resolving their ``#/$defs/<Name>`` references.
    schema_defs: dict[str, dict[str, typing.Any]] = pydantic.Field(default_factory=dict)

    @property
    def tool_exposed(self) -> bool:
        """True iff ``x-mcp-integration.tool`` is present."""
        return self.x_mcp_integration.tool is not None

    @property
    def resource_exposed(self) -> bool:
        """True iff ``x-mcp-integration.resource`` is present."""
        return self.x_mcp_integration.resource is not None


class OpenAPISpec(pydantic.BaseModel):
    """Decoded OpenAPI document with flattened operations and component schemas."""

    raw: dict[str, typing.Any]
    title: str = ''
    version: str = ''
    description: str = ''
    servers: list[dict[str, typing.Any]] = pydantic.Field(default_factory=list)
    operations: list[OperationInfo] = pydantic.Field(default_factory=list)
    security_schemes: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @property
    def default_base_url(self) -> str | None:
        """Return ``servers[0].url`` if present, else ``None``."""
        if self.servers:
            return self.servers[0].get('url')
        return None


def load_spec(source: str | pathlib.Path) -> dict[str, typing.Any]:
    """Load a JSON or YAML OpenAPI document from a filesystem path or HTTP(S) URL."""
    source_str = str(source)

    if source_str.startswith(('http://', 'https://')):
        logger.debug('Fetching OpenAPI spec from URL: %s', source_str)
        response = httpx.get(source_str, timeout=30, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '')
        if 'yaml' in content_type or 'yml' in content_type:
            return yaml.safe_load(response.text)
        try:
            return response.json()
        except json.JSONDecodeError:
            return yaml.safe_load(response.text)

    path = pathlib.Path(source_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f'OpenAPI spec not found: {path}')

    logger.debug('Loading OpenAPI spec from file: %s', path)
    text = path.read_text(encoding='utf-8')
    return yaml.safe_load(text) if path.suffix in ('.yaml', '.yml') else json.loads(text)


def _resolve_ref(raw: dict[str, typing.Any], ref: str) -> dict[str, typing.Any]:
    """Resolve a ``#/foo/bar`` JSON Pointer into ``raw``, returning ``{}`` if missing."""
    parts = ref.lstrip('#/').split('/')
    node = raw
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node


def _deep_merge(
    base: dict[str, typing.Any],
    override: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]] | None = None,
) -> dict[str, typing.Any]:
    """Recursively merge ``override`` into ``base``.

    ``required`` lists are concatenated and deduped instead of overwritten,
    so ``allOf`` chains accumulate every required property along the way.

    With ``defs``, a ``$defs`` reference meeting anything but the same reference is opened into its entry first,
    so its fields are merged with the other side's rather than replaced by them, see ``_open_reference``.
    """
    if defs is not None:
        base, override = _open_reference(base, override, defs), _open_reference(override, base, defs)
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value, defs)
        elif key == 'required' and isinstance(result.get(key), list) and isinstance(value, list):
            result[key] = list(dict.fromkeys(result[key] + value))
        else:
            result[key] = value
    return result


def _siblings(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Return the keywords beside the ``$ref`` of ``schema``."""
    return {key: value for key, value in schema.items() if key != '$ref'}


def _open_reference(
    schema: dict[str, typing.Any],
    other: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
) -> dict[str, typing.Any]:
    """Return ``schema`` with its ``$defs`` reference replaced by the entry, when merging it with ``other`` needs that.

    A reference can only stay one when ``other`` refers to the same entry, so only their siblings differ.
    Otherwise a key-by-key merge would let one side's ``properties`` or reference replace the entry's wholesale,
    so the entry is merged in with the keywords beside the reference winning.
    An entry still expanding has nothing to open yet, so its reference stays.
    """
    name = defs_reference(schema)
    if name is None or not other or defs_reference(other) == name or defs.get(name, _EXPANDING) is _EXPANDING:
        return schema
    return _deep_merge(defs[name], _siblings(schema), defs)


def schema_description(schema: dict[str, typing.Any], defs: dict[str, dict[str, typing.Any]]) -> str:
    """Return the ``description`` of ``schema``, looking through a ``$defs`` reference at its root.

    A description beside the reference describes this use, so it wins over the entry's.
    """
    if schema.get('description'):
        return schema['description']
    name = defs_reference(schema)
    return defs.get(name, {}).get('description', '') if name is not None else ''


def _normalize_nullable(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Rewrite an OpenAPI 3.0 ``nullable`` flag into the JSON Schema 2020-12 union form.

    ``{type: "X", nullable: true}`` becomes ``{type: ["X", "null"]}``,
    and the ``nullable`` keyword, which 2020-12 does not define, is dropped.
    A 3.1 ``type: ["X", "null"]`` is already correct and is left as is.
    """
    if 'nullable' not in schema:
        return schema
    result = {key: value for key, value in schema.items() if key != 'nullable'}
    if schema['nullable']:
        type_value = result.get('type')
        if isinstance(type_value, str):
            result['type'] = [type_value, 'null']
        elif isinstance(type_value, list) and 'null' not in type_value:
            result['type'] = [*type_value, 'null']
    return result


def _normalize_exclusive_bounds(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Rewrite OpenAPI 3.0 boolean ``exclusiveMinimum`` and ``exclusiveMaximum`` into 2020-12 numbers.

    In 3.0 a ``true`` flag pairs with ``minimum`` or ``maximum`` to mark the bound as exclusive.
    In 2020-12, which 3.1 already uses, the exclusive keyword holds the number itself,
    so the paired bound folds into it.
    A ``false`` flag only marks an inclusive bound, so it is dropped and the bound stays.
    """
    result = schema
    for exclusive_key, bound_key in (('exclusiveMinimum', 'minimum'), ('exclusiveMaximum', 'maximum')):
        if isinstance(result.get(exclusive_key), bool):
            if result is schema:
                result = dict(schema)
            if result[exclusive_key] and bound_key in result:
                result[exclusive_key] = result.pop(bound_key)
            else:
                del result[exclusive_key]
    return result


def _normalize_to_2020_12(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Rewrite OpenAPI 3.0 keywords into their JSON Schema 2020-12 equivalents.

    MCP advertises tool input schemas as 2020-12, which strict clients validate.
    A 3.0 construct left in place fails the call.
    A 3.1 schema is already 2020-12 and passes through unchanged.
    """
    return _normalize_exclusive_bounds(_normalize_nullable(schema))


def _truncate_at_cycle(schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Describe ``schema`` without any of the keywords that could lead back into it.

    A component schema that refers to itself is expressed through ``$defs`` and never reaches here.
    What does is a cycle that can only be described by inlining itself,
    such as an ``allOf`` that comes back round to its own schema, or a pointer into the middle of a component.
    Stopping here keeps every scalar keyword the fragment carries, ``type`` and ``title`` and the rest,
    and drops only the keywords that nest further,
    so the fragment still advertises itself as an object rather than collapsing to "anything".
    """
    dropped = {'$ref', 'allOf', *_SUBSCHEMA_KEYS, *_SUBSCHEMA_LIST_KEYS, *_SUBSCHEMA_MAP_KEYS}
    return {key: value for key, value in schema.items() if key not in dropped}


def _component_name(pointer: str) -> str | None:
    """Return the name of the component schema ``pointer`` addresses, or ``None`` when it addresses anything else.

    A pointer into the middle of a component, or a name outside the grammar OpenAPI allows, returns ``None``,
    so that fragment is inlined rather than referenced.
    """
    if not pointer.startswith(_COMPONENT_SCHEMA_PREFIX):
        return None
    name = pointer.removeprefix(_COMPONENT_SCHEMA_PREFIX)
    return name if _COMPONENT_NAME.match(name) else None


def iter_subschemas(schema: dict[str, typing.Any]) -> typing.Iterator[dict[str, typing.Any]]:
    """Yield every schema nested one level below ``schema``, through each nested-schema keyword.

    ``allOf`` is included, unlike in the expansion keyword lists, since a walk has to see every branch.
    """
    for key in _SUBSCHEMA_KEYS:
        if isinstance(schema.get(key), dict):
            yield schema[key]
    for key in (*_SUBSCHEMA_LIST_KEYS, 'allOf'):
        if isinstance(schema.get(key), list):
            yield from (item for item in schema[key] if isinstance(item, dict))
    for key in _SUBSCHEMA_MAP_KEYS:
        if isinstance(schema.get(key), dict):
            yield from (sub for sub in schema[key].values() if isinstance(sub, dict))


def map_subschemas(
    schema: dict[str, typing.Any],
    transform: typing.Callable[[dict[str, typing.Any]], dict[str, typing.Any]],
) -> dict[str, typing.Any]:
    """Return a copy of ``schema`` with ``transform`` applied to every schema nested one level below it.

    Walks the same keywords as ``iter_subschemas``, leaving any non-schema value in their place untouched.
    """
    result = schema.copy()
    for key in _SUBSCHEMA_KEYS:
        if isinstance(result.get(key), dict):
            result[key] = transform(result[key])
    for key in (*_SUBSCHEMA_LIST_KEYS, 'allOf'):
        if isinstance(result.get(key), list):
            result[key] = [transform(item) if isinstance(item, dict) else item for item in result[key]]
    for key in _SUBSCHEMA_MAP_KEYS:
        if isinstance(result.get(key), dict):
            result[key] = {name: transform(sub) if isinstance(sub, dict) else sub for name, sub in result[key].items()}
    return result


def defs_reference(schema: dict[str, typing.Any]) -> str | None:
    """Return the name of the ``$defs`` entry ``schema`` refers to, or ``None`` when it is not such a reference."""
    pointer = schema.get('$ref')
    if isinstance(pointer, str) and pointer.startswith(DEFS_REF_PREFIX):
        return pointer.removeprefix(DEFS_REF_PREFIX)
    return None


def _expand_inline(
    raw: dict[str, typing.Any],
    schema: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
    expanding: frozenset[str] = frozenset(),
) -> dict[str, typing.Any]:
    """Expand ``schema`` like ``_expand_schema``, but resolve a ``$ref`` at its root in place.

    For the places a reference cannot stand: an ``allOf`` branch, which is merged key by key,
    a request body, whose properties become parameters, and a component's own ``$defs`` entry.
    References below the root still become ``$defs`` references.

    ``expanding`` holds the pointers resolved in place on the current path, and callers leave it empty.
    Re-entering one means the schema can only be described by inlining itself, so that branch is truncated.
    The set is passed down rather than mutated, so it holds only the current path.
    """
    if '$ref' not in schema:
        return _expand_schema(raw, schema, defs, expanding)
    pointer = schema['$ref']
    resolved = _resolve_ref(raw, pointer)
    if pointer in expanding:
        expanded = _truncate_at_cycle(resolved)
    else:
        expanded = _expand_inline(raw, resolved, defs, expanding | {pointer})
    siblings = _siblings(schema)
    if not siblings:
        return expanded
    return _deep_merge(expanded, _expand_schema(raw, siblings, defs, expanding), defs)


def _expand_schema(
    raw: dict[str, typing.Any],
    schema: dict[str, typing.Any],
    defs: dict[str, dict[str, typing.Any]],
    expanding: frozenset[str] = frozenset(),
) -> dict[str, typing.Any]:
    """Expand a JSON Schema fragment into the form the gateway advertises.

    A ``$ref`` to a component schema stays a reference, rewritten to ``#/$defs/<Name>``,
    and the component itself is expanded once into ``defs`` under its name.
    Keywords beside the reference, which OpenAPI 3.1 allows, are kept beside it, since they describe this use.
    So one shape reached through several paths can be described once rather than once per path,
    and a component that refers to itself needs no truncation, since the reference stops at its own entry.
    Whether an entry is advertised under ``$defs`` or inlined back is decided per tool, by ``build_input_schema``.
    Any other ``$ref`` is resolved in place.

    ``allOf`` is flattened via ``_deep_merge``, opening a reference wherever two branches meet on it,
    then every nested-schema keyword is recursed into so a construct buried at any depth is expanded too,
    and finally OpenAPI 3.0 keywords are rewritten into their JSON Schema 2020-12 equivalents.
    Recursion keys off each keyword's presence rather than off ``type``,
    since ``type`` is optional and a fragment may carry ``properties`` or ``items`` without declaring it.

    ``defs`` is shared across a whole spec, so each component is expanded once however many operations reach it.
    """
    if '$ref' in schema:
        name = _component_name(schema['$ref'])
        if name is None:
            return _expand_inline(raw, schema, defs, expanding)
        if name not in defs:
            # Reserve the entry first, so a reference back into the component while it expands stops here.
            defs[name] = _EXPANDING
            # A component is a document of its own, so the in-place path of whoever reached it does not carry over.
            defs[name] = _expand_inline(raw, {'$ref': schema['$ref']}, defs)
        reference = {'$ref': DEFS_REF_PREFIX + name}
        siblings = _siblings(schema)
        if not siblings:
            return reference
        return {**reference, **_expand_schema(raw, siblings, defs, expanding)}

    if 'allOf' in schema:
        merged: dict[str, typing.Any] = {}
        for branch in schema['allOf']:
            merged = _deep_merge(merged, _expand_inline(raw, branch, defs, expanding), defs)
        return merged

    result = map_subschemas(schema, lambda subschema: _expand_schema(raw, subschema, defs, expanding))
    return _normalize_to_2020_12(result)


def count_defs_references(
    schemas: typing.Iterable[dict[str, typing.Any]],
    defs: dict[str, dict[str, typing.Any]],
) -> collections.Counter[str]:
    """Count the places in ``schemas`` that reach each entry of ``defs``, directly or through other entries.

    An entry's own references are counted once, however often the entry is reached,
    since the entry is written out once, whether under ``$defs`` or inlined at its only use.
    The entries counted are exactly those ``schemas`` reach.
    """
    counts: collections.Counter[str] = collections.Counter()
    pending = list(schemas)
    while pending:
        node = pending.pop()
        name = defs_reference(node)
        if name is not None and name in defs:
            if name not in counts:
                pending.append(defs[name])
            counts[name] += 1
        pending.extend(iter_subschemas(node))
    return counts


def _declares_object_properties(schema: dict[str, typing.Any]) -> bool:
    """Report whether ``schema`` is an object whose ``properties`` become body parameters.

    Keying off ``properties`` rather than ``type`` matches ``_expand_schema``, since ``type`` is optional,
    and normalization turns a nullable object into ``{type: ["object", "null"]}``,
    so an equality test against ``"object"`` would drop every body field of an optional request.
    A declared non-object ``type`` still wins.
    """
    if not schema.get('properties'):
        return False
    schema_type = schema.get('type')
    if schema_type is None:
        return True
    if isinstance(schema_type, list):
        return 'object' in schema_type
    return schema_type == 'object'


def _resolve_relative_servers(servers: list[dict[str, typing.Any]], source: str | None) -> list[dict[str, typing.Any]]:
    """Resolve relative ``servers[].url`` against ``source`` per OpenAPI 3.0 §4.7.5."""
    if not source or not source.startswith(('http://', 'https://')):
        return servers
    resolved: list[dict[str, typing.Any]] = []
    for server in servers:
        url = server.get('url', '')
        if url and not url.startswith(('http://', 'https://')):
            resolved.append({**server, 'url': urllib.parse.urljoin(source, url)})
        else:
            resolved.append(server)
    return resolved


def parse_spec(raw: dict[str, typing.Any], source: str | None = None) -> OpenAPISpec:
    """Parse a decoded OpenAPI mapping into ``OpenAPISpec`` and ``OperationInfo`` models.

    ``source`` is only used to resolve relative ``servers[].url`` values per OpenAPI 3.0 §4.7.5,
    so pass the original URL or path the document was loaded from when it matters.
    """
    info = raw.get('info', {})
    servers = _resolve_relative_servers(raw.get('servers', []), source)
    security_schemes = raw.get('components', {}).get('securitySchemes', {})
    global_security = raw.get('security', [])

    operations: list[OperationInfo] = []
    # Every component expanded so far, shared by all operations so each is expanded once.
    defs: dict[str, dict[str, typing.Any]] = {}

    for path, path_item in raw.get('paths', {}).items():
        if not isinstance(path_item, dict):
            continue

        for method in ('get', 'post', 'put', 'patch', 'delete'):
            operation = path_item.get(method)
            if not operation or not isinstance(operation, dict):
                continue

            operation_id = operation.get('operationId')
            if not operation_id:
                operation_id = f'{method}_{path}'.replace('/', '_').replace('{', '').replace('}', '').strip('_')

            # Operation-level parameters override path-level (OpenAPI 3.0 §4.7.9.2).
            params: list[ParameterInfo] = []
            seen_params: set[str] = set()
            for param in operation.get('parameters', []) + path_item.get('parameters', []):
                if '$ref' in param:
                    param = _resolve_ref(raw, param['$ref'])
                param_key = f'{param.get("in", "query")}:{param["name"]}'
                if param_key in seen_params:
                    continue
                seen_params.add(param_key)
                param_schema = _expand_schema(raw, param.get('schema', {}), defs)
                params.append(
                    ParameterInfo(
                        name=param['name'],
                        location=param.get('in', 'query'),
                        required=param.get('required', False),
                        description=param.get('description', ''),
                        schema=param_schema,
                    )
                )

            request_body = operation.get('requestBody', {})
            if '$ref' in request_body:
                request_body = _resolve_ref(raw, request_body['$ref'])
            if request_body and method in ('post', 'put', 'patch'):
                content = request_body.get('content', {}).get('application/json', {})
                # The body is split into one parameter per property, so its own root reference is resolved in place.
                schema = _expand_inline(raw, content.get('schema', {}), defs)

                if _declares_object_properties(schema):
                    required_props = schema.get('required', [])
                    for prop_name, prop_schema in schema['properties'].items():
                        params.append(
                            ParameterInfo(
                                name=prop_name,
                                location='body',
                                required=prop_name in required_props,
                                description=schema_description(prop_schema, defs),
                                schema=prop_schema,
                            )
                        )

            operations.append(
                OperationInfo(
                    operation_id=operation_id,
                    method=method,
                    path=path,
                    summary=operation.get('summary', ''),
                    description=operation.get('description', ''),
                    tags=operation.get('tags', []),
                    parameters=params,
                    security=operation.get('security', global_security),
                    x_mcp_integration=operation.get('x-mcp-integration', {}),
                    schema_defs={
                        name: defs[name]
                        for name in count_defs_references((parameter.schema_ for parameter in params), defs)
                    },
                )
            )

    return OpenAPISpec(
        raw=raw,
        title=info.get('title', ''),
        version=info.get('version', ''),
        description=info.get('description', ''),
        servers=servers,
        operations=operations,
        security_schemes=security_schemes,
    )
