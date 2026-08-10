import json
import logging
import pathlib
import typing
import urllib.parse

import httpx
import pydantic
import yaml


logger = logging.getLogger(__name__)


class ParameterInfo(pydantic.BaseModel):
    """One OpenAPI parameter (path, query, header, cookie, or body) with its schema."""

    name: str
    location: typing.Literal['path', 'query', 'header', 'cookie', 'body']
    required: bool = False
    description: str = ''
    schema_type: str = 'string'
    schema_: dict[str, typing.Any] = pydantic.Field(default_factory=dict, alias='schema')
    # Set by shape_operation from a ParamOverride default.
    # Marks a default the author wants sent upstream even when the LLM omits the parameter.
    # The LLM never sees this flag.
    send_default: bool = False
    # Set False by shape_operation for a hidden-but-defaulted parameter,
    # which is kept for upstream assembly and default injection but omitted from the schema.
    visible: bool = True

    model_config = pydantic.ConfigDict(populate_by_name=True)


class ExposedTool(typing.NamedTuple):
    """One tool a server exposes, with a compact label of how it is shaped."""

    name: str
    method: str
    path: str
    shaping: str


class ParamOverride(pydantic.BaseModel):
    """One LLM-facing parameter from ``x-mcp-integration.tool.params.<name>``, keyed by the friendly name.

    Every key other than the two meta-flags is a JSON Schema keyword
    (``type``, ``enum``, ``format``, ``default``, ``description``, ``minimum`` and so on),
    describing the value exactly as it appears in the tool's advertised input schema.

    How the entry is applied depends on ``ToolOverride.strategy``.
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

    ``params`` shapes the LLM-facing input schema, and ``strategy`` says how it relates to the spec.
    With ``merge`` the entries tweak existing spec parameters and the rest stay visible.
    With ``replace`` the entries are the whole surface and every undeclared spec parameter is dropped.
    ``strategy`` is required whenever ``params`` is set.

    ``request`` and ``response`` are JSONata expressions that transform the values,
    ``request`` mapping the friendly arguments into the upstream request,
    and ``response`` mapping the upstream response into what the client sees.
    """

    name: str | None = None
    description: str | None = None
    annotations: dict[str, typing.Any] | None = None
    params: dict[str, ParamOverride] = pydantic.Field(default_factory=dict)
    strategy: typing.Literal['merge', 'replace'] | None = None
    request: str | None = None
    response: str | None = None


class ResourceOverride(pydantic.BaseModel):
    """Spec-author overrides for the MCP resource generated from an operation.

    ``uri_template`` overrides the auto-derived URI when set,
    and must start with ``{server_name}://`` so resources stay scoped to the owning server.
    """

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


def _deep_merge(base: dict[str, typing.Any], override: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Recursively merge ``override`` into ``base``.

    ``required`` lists are concatenated and deduped instead of overwritten,
    so ``allOf`` chains accumulate every required property along the way.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key == 'required' and isinstance(result.get(key), list) and isinstance(value, list):
            result[key] = list(dict.fromkeys(result[key] + value))
        else:
            result[key] = value
    return result


def _type_names(type_value: typing.Any) -> set[str]:
    """Return the type names in a JSON Schema ``type``.

    OpenAPI 3.0 writes ``type`` as one string, 3.1 writes it as a list of strings.
    Reading both into a set lets the recursion below match either form.
    """
    if isinstance(type_value, str):
        return {type_value}
    if isinstance(type_value, list):
        return {entry for entry in type_value if isinstance(entry, str)}
    return set()


def _primary_type(type_value: typing.Any) -> str:
    """Reduce a possibly-array JSON Schema ``type`` to one representative string, ignoring ``null``.

    OpenAPI 3.0 gives one string, so this is a no-op there.
    OpenAPI 3.1 may give a list like ``["integer", "null"]``.
    Only one string fits the ``schema_type`` fallback field, so this keeps one non-null member,
    and the full array stays available on the parameter's ``schema``.
    """
    if isinstance(type_value, list):
        non_null = [entry for entry in type_value if entry != 'null']
        return non_null[0] if non_null else 'string'
    return type_value if isinstance(type_value, str) else 'string'


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


def _expand_schema(raw: dict[str, typing.Any], schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Expand a JSON Schema fragment in place.

    Resolves ``$ref``, flattens ``allOf`` via ``_deep_merge``,
    recurses into ``properties`` / ``items`` / ``additionalProperties`` / ``oneOf`` / ``anyOf``,
    and rewrites OpenAPI 3.0 keywords into their JSON Schema 2020-12 equivalents.
    """
    if '$ref' in schema:
        resolved = _resolve_ref(raw, schema['$ref'])
        return _expand_schema(raw, resolved)

    if 'allOf' in schema:
        merged: dict[str, typing.Any] = {}
        for sub in schema['allOf']:
            expanded = _expand_schema(raw, sub)
            merged = _deep_merge(merged, expanded)
        return merged

    result = schema.copy()

    type_names = _type_names(result.get('type'))
    if 'object' in type_names and result.get('properties'):
        result['properties'] = {k: _expand_schema(raw, v) for k, v in result['properties'].items()}
    elif 'array' in type_names and result.get('items'):
        result['items'] = _expand_schema(raw, result['items'])

    if isinstance(result.get('additionalProperties'), dict):
        result['additionalProperties'] = _expand_schema(raw, result['additionalProperties'])
    if 'oneOf' in result:
        result['oneOf'] = [_expand_schema(raw, item) for item in result['oneOf']]
    if 'anyOf' in result:
        result['anyOf'] = [_expand_schema(raw, item) for item in result['anyOf']]

    return _normalize_to_2020_12(result)


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
                param_schema = _expand_schema(raw, param.get('schema', {}))
                params.append(
                    ParameterInfo(
                        name=param['name'],
                        location=param.get('in', 'query'),
                        required=param.get('required', False),
                        description=param.get('description', ''),
                        schema_type=_primary_type(param_schema.get('type', 'string')),
                        schema=param_schema,
                    )
                )

            request_body = operation.get('requestBody', {})
            if '$ref' in request_body:
                request_body = _resolve_ref(raw, request_body['$ref'])
            if request_body and method in ('post', 'put', 'patch'):
                content = request_body.get('content', {}).get('application/json', {})
                schema = _expand_schema(raw, content.get('schema', {}))

                if schema.get('type') == 'object' and schema.get('properties'):
                    required_props = schema.get('required', [])
                    for prop_name, prop_schema in schema['properties'].items():
                        params.append(
                            ParameterInfo(
                                name=prop_name,
                                location='body',
                                required=prop_name in required_props,
                                description=prop_schema.get('description', ''),
                                schema_type=_primary_type(prop_schema.get('type', 'string')),
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
