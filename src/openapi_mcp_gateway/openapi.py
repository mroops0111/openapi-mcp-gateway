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
    """Parsed parameter from an OpenAPI operation."""

    name: str
    location: typing.Literal['path', 'query', 'header', 'cookie', 'body']
    required: bool = False
    description: str = ''
    schema_type: str = 'string'
    schema_: dict[str, typing.Any] = pydantic.Field(default_factory=dict, alias='schema')

    model_config = pydantic.ConfigDict(populate_by_name=True)


class OperationInfo(pydantic.BaseModel):
    """Parsed operation from an OpenAPI specification."""

    operation_id: str
    method: str
    path: str
    summary: str = ''
    description: str = ''
    tags: list[str] = pydantic.Field(default_factory=list)
    parameters: list[ParameterInfo] = pydantic.Field(default_factory=list)
    security: list[dict[str, list[str]]] = pydantic.Field(default_factory=list)
    x_mcp_integration: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @property
    def tool_exposed(self) -> bool:
        return 'tool' in self.x_mcp_integration.get('expose', {})


class OpenAPISpec(pydantic.BaseModel):
    """Parsed OpenAPI specification."""

    raw: dict[str, typing.Any]
    title: str = ''
    version: str = ''
    description: str = ''
    servers: list[dict[str, typing.Any]] = pydantic.Field(default_factory=list)
    operations: list[OperationInfo] = pydantic.Field(default_factory=list)
    security_schemes: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @property
    def default_base_url(self) -> str | None:
        if self.servers:
            return self.servers[0].get('url')
        return None


def load_spec(source: str | pathlib.Path) -> dict[str, typing.Any]:
    """Load an OpenAPI spec from a local file path or a URL.

    Supports JSON and YAML formats.
    """
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
    """Resolve a JSON $ref pointer (e.g. #/components/schemas/Foo)."""
    parts = ref.lstrip('#/').split('/')
    node = raw
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node


def _deep_merge(base: dict[str, typing.Any], override: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Deep merge two dicts. For 'required' lists, concatenate instead of replace."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key == 'required' and isinstance(result.get(key), list) and isinstance(value, list):
            result[key] = list(dict.fromkeys(result[key] + value))
        else:
            result[key] = value
    return result


def _expand_schema(raw: dict[str, typing.Any], schema: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Recursively expand a schema, resolving $ref, allOf, oneOf, anyOf."""
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

    if result.get('type') == 'object' and result.get('properties'):
        result['properties'] = {k: _expand_schema(raw, v) for k, v in result['properties'].items()}
    elif result.get('type') == 'array' and result.get('items'):
        result['items'] = _expand_schema(raw, result['items'])

    if 'oneOf' in result:
        result['oneOf'] = [_expand_schema(raw, item) for item in result['oneOf']]
    if 'anyOf' in result:
        result['anyOf'] = [_expand_schema(raw, item) for item in result['anyOf']]

    return result


def _resolve_relative_servers(servers: list[dict[str, typing.Any]], source: str | None) -> list[dict[str, typing.Any]]:
    """Resolve relative server URLs against the spec's source URL (OpenAPI 3.0 §4.7.5)."""
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
    """Parse a raw OpenAPI dict into structured data.

    Args:
        raw: The decoded OpenAPI document.
        source: Optional URL the spec was loaded from. Used to resolve relative
            ``servers[].url`` entries per OpenAPI 3.0 §4.7.5.
    """
    info = raw.get('info', {})
    servers = _resolve_relative_servers(raw.get('servers', []), source)
    security_schemes = raw.get('components', {}).get('securitySchemes', {})
    global_security = raw.get('security', [])

    operations: list[OperationInfo] = []

    for path, path_item in raw.get('paths', {}).items():
        if not isinstance(path_item, dict):
            continue

        for method in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
            operation = path_item.get(method)
            if not operation or not isinstance(operation, dict):
                continue

            operation_id = operation.get('operationId')
            if not operation_id:
                # Generate one from method + path
                operation_id = f'{method}_{path}'.replace('/', '_').replace('{', '').replace('}', '').strip('_')

            # Parse parameters (operation-level overrides path-level per OpenAPI spec)
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
                        schema_type=param_schema.get('type', 'string'),
                        schema=param_schema,
                    )
                )

            # Parse request body as body parameters
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
                                schema_type=prop_schema.get('type', 'string'),
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
