import collections
import dataclasses
import json
import logging
import pathlib
import re
import types
import typing
import urllib.parse

import httpx
import pydantic
import yaml

from .field_path import FieldMatch, FieldPath


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

# Keywords whose value is a list of schemas the instance is composed from, each searched at the same field path.
_COMPOSITION_KEYWORDS = ('allOf', 'oneOf', 'anyOf')
# The media type a base64 field is promoted as when nothing names one.
_DEFAULT_MEDIA_TYPE = 'application/octet-stream'
# The keywords a walk for media descends through, each of which beside a reference replaces the target's.
_MEDIA_WALK_KEYWORDS = frozenset({'properties', 'items', *_COMPOSITION_KEYWORDS})

# Whatever ``_distinct`` dedupes, kept as the type it returns.
_Item = typing.TypeVar('_Item')

# The YAML loader for specs, the libyaml one when PyYAML was built with it.
# It reads the same documents as the pure Python loader nearly ten times faster, which matters for a spec of megabytes.
_YAML_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)


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

    ``OperationInfo`` is the operation as the spec declares it.
    This is what the model is shown after naming, filtering and shaping have run,
    so the names here can differ from the spec's.

    ``input_schema`` is the advertised schema itself rather than a summary of it,
    because a flattened list loses the nested body properties, enums, defaults and bounds a reviewer needs most.

    ``shaping`` reports what took effect rather than what was declared, and is ``None`` when nothing reshapes the call.
    A ``params_strategy`` with no ``params`` changes nothing.
    """

    name: str
    method: str
    path: str
    shaping: dict[str, typing.Any] | None = None
    description: str = ''
    input_schema: dict[str, typing.Any] | None = None


class ParamOverride(pydantic.BaseModel):
    """One LLM-facing parameter from ``x-mcp-integration.tool.params.<name>``, keyed by the friendly name.

    Every key other than the two meta-flags is a JSON Schema keyword, such as ``type``, ``enum``, ``format``,
    ``default``, ``description`` or ``minimum``,
    describing the value exactly as it appears in the tool's advertised input schema.

    How the entry is applied depends on ``ToolOverride.params_strategy``.
    An entry carrying a ``type`` declares the parameter's schema,
    either replacing a matching spec parameter's schema or introducing a brand-new friendly parameter.
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


class MediaField(pydantic.BaseModel):
    """How one response field carries media, so the gateway can promote it to an MCP content block.

    ``encoding`` says how to get the bytes out of the field, and is the only thing that varies by source.
    Only ``base64`` exists today.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    encoding: typing.Literal['base64']
    media_type: str = _DEFAULT_MEDIA_TYPE


class ToolOverride(pydantic.BaseModel):
    """Spec-author overrides for the MCP tool generated from an operation.

    ``params`` shapes the LLM-facing input schema, and ``params_strategy`` says how it relates to the spec.
    With ``merge`` the entries tweak existing spec parameters and the rest stay visible.
    With ``replace`` the entries are the whole surface and every undeclared spec parameter is dropped.
    ``params_strategy`` is required whenever ``params`` is set.

    ``request`` and ``response`` are JSONata expressions that transform the values,
    ``request`` mapping the friendly arguments into the upstream request,
    and ``response`` mapping the upstream response into what the client sees.

    ``media`` maps a field path in the upstream response, such as ``attachments[*].content``, to how it carries media.
    Each entry wins over what the spec declares for the same path.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    name: str | None = None
    description: str | None = None
    annotations: dict[str, typing.Any] | None = None
    params: dict[str, ParamOverride] = pydantic.Field(default_factory=dict)
    params_strategy: typing.Literal['merge', 'replace'] | None = None
    request: str | None = None
    response: str | None = None
    media: dict[str, MediaField] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator('media')
    @classmethod
    def _validate_media_paths(cls, media: dict[str, MediaField]) -> dict[str, MediaField]:
        """Reject a malformed field path when the config loads, rather than letting it match nothing."""
        for path in media:
            FieldPath.parse(path)
        return media


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


class ResponseMedia:
    """Where a success response schema declares ``contentEncoding: base64``, to find in each response at call time.

    The schema is kept only as far as it leads to such a field, and its references point into ``components``,
    which every operation of a spec shares.
    A walk with it moves one response value down per step, so a recursive schema costs no more than the response.
    It is never changed, so a deep copy of the operation holding it shares it rather than copying the components.
    """

    def __init__(self, root: '_MediaNode', components: typing.Mapping[str, '_MediaNode']):
        self._root = root
        self._components = components

    def __copy__(self) -> typing.Self:
        return self

    def __deepcopy__(self, memo: dict[int, typing.Any]) -> typing.Self:
        return self

    def find(self, payload: typing.Any) -> typing.Iterator[tuple[FieldMatch, MediaField]]:
        """Yield every value of ``payload`` the schema declares as base64, with how it carries media.

        Values come in payload order, each once, typed by the first composed branch that declares it base64.
        The payload itself is never yielded, since a placeholder needs a field to stand in.
        """
        return self._find((self._root,), payload, None)

    def _find(
        self,
        nodes: typing.Sequence['_MediaNode'],
        value: typing.Any,
        match: FieldMatch | None,
    ) -> typing.Iterator[tuple[FieldMatch, MediaField]]:
        """Yield the media under ``value``, which every one of ``nodes`` describes and ``match`` holds.

        Each value is visited once with all the schemas that describe it,
        so composed branches that meet again at the same component cost nothing more.
        """
        chains = self._chains(nodes)
        if match is not None:
            for chain in chains:
                if next((link.encoding for link in chain if link.encoding is not None), None) == 'base64':
                    media_type = next(
                        (link.media_type for link in chain if link.media_type is not None), _DEFAULT_MEDIA_TYPE
                    )
                    yield match, MediaField(encoding='base64', media_type=media_type)
                    break
        if isinstance(value, dict):
            declared = _distinct(_declaring(chain, 'properties').properties or None for chain in chains)
            if not declared:
                return
            names = declared[0].keys() & value.keys()
            for properties in declared[1:]:
                names |= properties.keys() & value.keys()
            # Most values hold at most one field leading to media, which needs no ordering.
            for name in names if len(names) < 2 else [name for name in value if name in names]:
                children = _distinct(properties.get(name) for properties in declared)
                path = f'{match.path}.{name}' if match is not None else name
                yield from self._find(children, value[name], FieldMatch(value, name, path))
        elif isinstance(value, list):
            children = _distinct(_declaring(chain, 'items').items for chain in chains)
            if children:
                prefix = match.path if match is not None else ''
                for index, item in enumerate(value):
                    yield from self._find(children, item, FieldMatch(value, index, f'{prefix}[{index}]'))

    def _chains(self, nodes: typing.Sequence['_MediaNode']) -> list[list['_MediaNode']]:
        """Return each distinct schema that ``nodes`` and their composed branches read as at one value.

        Chains come in the order a depth-first walk of the branches meets them.
        Two that differ only in links declaring nothing read alike, so the second is dropped,
        which also ends a branch that loops back to a schema already read here.
        """
        if len(nodes) == 1:
            chain = self._chain(nodes[0])
            if not any(link.branches for link in chain):
                return [chain]
        chains: list[list[_MediaNode]] = []
        seen: set[tuple[int, ...]] = set()
        pending = list(reversed(nodes))
        while pending:
            chain = self._chain(pending.pop())
            key = tuple(id(link) for link in chain if link is chain[-1] or link.declares_any)
            if key in seen:
                continue
            seen.add(key)
            chains.append(chain)
            pending.extend(
                reversed(
                    [
                        branch
                        for keyword in _COMPOSITION_KEYWORDS
                        for branch_keyword, branch in _declaring(chain, keyword).branches
                        if branch_keyword == keyword
                    ]
                )
            )
        return chains

    def _chain(self, node: '_MediaNode') -> list['_MediaNode']:
        """Return ``node`` followed by the components its references lead to, which together it reads as.

        Each keyword is taken from the nearest of them that declares it.
        Only a reference this chain already followed is a loop.
        A branch referring back to a component whose keywords a reference beside it replaced reads it in full.
        """
        chain = [node]
        followed: set[str] = set()
        while (reference := chain[-1].reference) is not None and reference not in followed:
            target = self._components.get(reference)
            if target is None:
                break
            followed.add(reference)
            chain.append(target)
        return chain


class OperationInfo(pydantic.BaseModel):
    """One HTTP operation from ``paths`` with parameters, security, and MCP integration flags."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

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
    # Where the success response schema declares media, or ``None`` when it declares none anywhere.
    response_media: ResponseMedia | None = None

    @property
    def tool_exposed(self) -> bool:
        """True iff ``x-mcp-integration.tool`` is present."""
        return self.x_mcp_integration.tool is not None

    @property
    def resource_exposed(self) -> bool:
        """True iff ``x-mcp-integration.resource`` is present."""
        return self.x_mcp_integration.resource is not None


class OpenAPISpec(pydantic.BaseModel):
    """The metadata, flattened operations, and security schemes the gateway keeps of a parsed OpenAPI document.

    The decoded document itself is not kept, since every reference is resolved while parsing,
    and it is the largest part.
    """

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
            return _load_yaml(response.text)
        try:
            return response.json()
        except json.JSONDecodeError:
            return _load_yaml(response.text)

    path = pathlib.Path(source_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f'OpenAPI spec not found: {path}')

    logger.debug('Loading OpenAPI spec from file: %s', path)
    text = path.read_text(encoding='utf-8')
    return _load_yaml(text) if path.suffix in ('.yaml', '.yml') else json.loads(text)


def _load_yaml(text: str) -> typing.Any:
    return yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506  # Both loaders are the safe variants.


def _resolve_ref(raw: dict[str, typing.Any], ref: str) -> dict[str, typing.Any]:
    """Resolve a ``#/foo/bar`` JSON Pointer into ``raw``, returning ``{}`` if missing or not a mapping."""
    parts = ref.lstrip('#/').split('/')
    node = raw
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


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

    This serves the places a reference cannot stand, which are an ``allOf`` branch, merged key by key,
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


def _success_response_schema(raw: dict[str, typing.Any], operation: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Return the JSON schema of the operation's first ``2xx`` response, or ``{}`` when it declares none."""
    # A YAML spec can key a status as an integer, so every key is read as the string the spec means.
    responses = {str(status): response for status, response in (operation.get('responses') or {}).items()}
    for status in sorted(responses):
        if not status.startswith('2') or not isinstance(responses[status], dict):
            continue
        response = responses[status]
        if '$ref' in response:
            response = _resolve_ref(raw, response['$ref'])
        for media_type, content in response.get('content', {}).items():
            if 'json' in media_type and isinstance(content, dict):
                return content.get('schema', {})
    return {}


class _ResponseMediaBuilder:
    """Build each operation's ``ResponseMedia`` for one spec, sharing the components every one of them refers to.

    A reference whose target cannot reach base64 is dropped, and so is any schema that leads only to such references.
    A component also keeps a reference to one it is an alias of,
    since its ``contentMediaType`` names the type of base64 declared beside a reference to the alias.
    """

    def __init__(self, raw: dict[str, typing.Any]):
        self._raw = raw
        targets = self._reference_graph(raw)
        self._holds_base64 = targets is not None
        self._reaching = self._references_reaching_base64(targets or {})
        self._reaching_or_aliased = self._with_aliased(raw, self._reaching)
        components: dict[str, _MediaNode] = {}
        for reference in self._reaching_or_aliased:
            node = self._node(_resolve_ref(raw, reference), component=True)
            if node is not None:
                components[reference] = node
        self._components = types.MappingProxyType(components)

    def build(self, schema: typing.Any) -> ResponseMedia | None:
        """Return where ``schema`` declares media, or ``None`` when it declares none at any depth."""
        if not self._holds_base64:
            return None
        root = self._node(schema)
        return None if root is None else ResponseMedia(root, self._components)

    def _node(self, schema: typing.Any, *, component: bool = False) -> '_MediaNode | None':
        """Keep what ``schema`` holds that leads to base64, not following a ``$ref``, or ``None`` when nothing does.

        A ``component`` keeps its ``contentMediaType`` even without base64,
        since a schema referring to it can declare base64 beside the reference.
        """
        if not isinstance(schema, dict):
            return None
        reference = schema.get('$ref')
        followable = self._reaching_or_aliased if component else self._reaching
        reference = reference if isinstance(reference, str) and reference in followable else None
        encoding, media_type = schema.get('contentEncoding'), schema.get('contentMediaType')
        properties = schema.get('properties')
        node = _MediaNode(
            encoding=encoding if isinstance(encoding, str) else None,
            media_type=media_type if isinstance(media_type, str) else None,
            properties=types.MappingProxyType(
                {
                    name: child
                    for name, property_schema in (properties.items() if isinstance(properties, dict) else ())
                    if (child := self._node(property_schema)) is not None
                }
            ),
            items=self._node(schema.get('items')),
            branches=tuple(
                (keyword, child)
                for keyword in _COMPOSITION_KEYWORDS
                if isinstance(schema.get(keyword), list)
                for branch in schema[keyword]
                if (child := self._node(branch)) is not None
            ),
            reference=reference,
            replaces=frozenset(_MEDIA_WALK_KEYWORDS & schema.keys()) if reference is not None else frozenset(),
        )
        leads_on = bool(node.properties or node.branches) or node.items is not None or reference is not None
        declares_media = node.encoding == 'base64' or (component and (node.encoding or node.media_type) is not None)
        return node if leads_on or declares_media else None

    @staticmethod
    def _reference_graph(raw: dict[str, typing.Any]) -> dict[str, tuple[frozenset[str], bool]] | None:
        """Map each reference in ``raw`` to the references its walk can follow next, and whether it meets base64.

        A walk that follows a reference goes on through its target,
        and also through the keywords beside it wherever it is used with some, since those replace the target's,
        so both count as the reference's own, which keeps every followed reference upstream of what its walk meets.
        Every reference found anywhere is an edge, rather than only those the walk follows,
        so the graph can only overstate what reaches base64, which costs speed but never a field.
        Returns ``None`` when ``raw`` declares no base64 anywhere, as most specs do, since then nothing is media.
        """
        every_reference: set[str] = set()
        beside: dict[str, set[str]] = collections.defaultdict(set)
        base64_beside: set[str] = set()
        holds_any_base64 = False
        for node in _iter_mappings(raw):
            holds_any_base64 = holds_any_base64 or node.get('contentEncoding') == 'base64'
            reference = node.get('$ref')
            if not isinstance(reference, str):
                continue
            every_reference.add(reference)
            if len(node) > 1:
                references, holds_base64 = _references_and_base64(_siblings(node))
                beside[reference] |= references
                if holds_base64:
                    base64_beside.add(reference)
        if not holds_any_base64:
            return None
        graph: dict[str, tuple[frozenset[str], bool]] = {}
        pending = list(every_reference)
        while pending:
            reference = pending.pop()
            if reference in graph:
                continue
            references, holds_base64 = _references_and_base64(_resolve_ref(raw, reference))
            references |= beside.get(reference, set())
            graph[reference] = (references, holds_base64 or reference in base64_beside)
            pending.extend(references - graph.keys())
        return graph

    @staticmethod
    def _with_aliased(raw: dict[str, typing.Any], references: frozenset[str]) -> frozenset[str]:
        """Return ``references`` with every reference their targets are aliases of, at any depth.

        A schema reads as one with its whole chain of aliases,
        so the ``contentMediaType`` of one that cannot reach base64 still names the type base64 beside it carries.
        """
        kept = set(references)
        pending = list(references)
        while pending:
            alias = _resolve_ref(raw, pending.pop()).get('$ref')
            if isinstance(alias, str) and alias not in kept:
                kept.add(alias)
                pending.append(alias)
        return frozenset(kept)

    @staticmethod
    def _references_reaching_base64(targets: dict[str, tuple[frozenset[str], bool]]) -> frozenset[str]:
        """Return the references whose target holds base64 or refers, at any depth, to one that does."""
        referrers: dict[str, list[str]] = collections.defaultdict(list)
        for reference, (references, _holds_base64) in targets.items():
            for target in references:
                referrers[target].append(reference)
        reaching = {reference for reference, (_references, holds_base64) in targets.items() if holds_base64}
        pending = list(reaching)
        while pending:
            for referrer in referrers[pending.pop()]:
                if referrer not in reaching:
                    reaching.add(referrer)
                    pending.append(referrer)
        return frozenset(reaching)


@dataclasses.dataclass(frozen=True, eq=False)
class _MediaNode:
    """The part of one schema that leads to base64, with each nested schema that does, by the keyword holding it.

    ``encoding`` and ``media_type`` are its ``contentEncoding`` and ``contentMediaType``, whatever they say.
    ``reference`` is the component it goes on to at the same value, a key of ``ResponseMedia``'s components,
    and ``replaces`` the walk keywords it declares beside that reference, which count rather than the component's.
    """

    encoding: str | None
    media_type: str | None
    properties: typing.Mapping[str, '_MediaNode']
    items: '_MediaNode | None'
    branches: tuple[tuple[str, '_MediaNode'], ...]
    reference: str | None
    replaces: frozenset[str]

    @property
    def declares_any(self) -> bool:
        """Whether it declares anything of its own, rather than only going on to its reference."""
        return self.encoding is not None or self.media_type is not None or bool(self.replaces)


def _declaring(chain: list[_MediaNode], keyword: str) -> _MediaNode:
    """Return the nearest schema of a reference ``chain`` whose ``keyword`` counts, the last when none replaces it."""
    for link in chain:
        if keyword in link.replaces:
            return link
    return chain[-1]


def _distinct(items: typing.Iterable[_Item | None]) -> tuple[_Item, ...]:
    """Return ``items`` without ``None`` or the same object twice, in order."""
    return tuple({id(item): item for item in items if item is not None}.values())


def _iter_mappings(node: typing.Any) -> typing.Iterator[dict[str, typing.Any]]:
    """Yield every mapping under ``node``, itself included, without following any ``$ref``."""
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            yield current
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _references_and_base64(node: typing.Any) -> tuple[frozenset[str], bool]:
    """Return every ``$ref`` under ``node``, not followed, and whether any schema under it declares base64."""
    references: set[str] = set()
    holds_base64 = False
    for mapping in _iter_mappings(node):
        reference = mapping.get('$ref')
        if isinstance(reference, str):
            references.add(reference)
        holds_base64 = holds_base64 or mapping.get('contentEncoding') == 'base64'
    return frozenset(references), holds_base64


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
    response_media = _ResponseMediaBuilder(raw)

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
                    response_media=response_media.build(_success_response_schema(raw, operation)),
                )
            )

    return OpenAPISpec(
        title=info.get('title', ''),
        version=info.get('version', ''),
        description=info.get('description', ''),
        servers=servers,
        operations=operations,
        security_schemes=security_schemes,
    )
