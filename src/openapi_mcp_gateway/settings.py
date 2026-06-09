import os
import pathlib
import re
import typing

import pydantic
import yaml

from .openapi import McpIntegration


def _deep_merge(base: dict[str, typing.Any], override: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Recursively merge ``override`` into ``base``; ``override`` wins for non-dict values.

    Keys present only in ``base`` are preserved.
    Keys whose value on both sides is a dict are merged recursively,
    so a partial ``{'logging': {'level': 'DEBUG'}}`` does not blow away the rest of ``logging``.
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_env_var(value: str | None) -> str | None:
    """Substitute ``${VAR}`` or ``${VAR:-default}`` when ``value`` is a lone reference,
    otherwise pass it through unchanged.

    Returns ``None`` if the variable is unset and no default is given.
    """
    if value is None:
        return None
    match = re.fullmatch(r'\$\{(\w+)(?::-(.*))?\}', value)
    if not match:
        return value
    env_value = os.environ.get(match.group(1))
    if env_value is not None:
        return env_value
    if match.group(2) is not None:
        return match.group(2)
    return None


class AuthConfig(pydantic.BaseModel):
    """Authentication for an upstream API.

    ``token``, ``client_id``, and ``client_secret`` accept ``${ENV_VAR}``
    and ``${ENV_VAR:-default}`` substitution at resolve time.
    Numeric OAuth credentials are coerced from int to str,
    so unquoted YAML values still parse on providers that use numeric ``client_id`` (Asana, Facebook).
    """

    model_config = pydantic.ConfigDict(coerce_numbers_to_str=True)

    type: typing.Literal['bearer', 'api_key', 'oauth2', 'none'] = 'none'
    token: str | None = None
    api_key_header: str = 'X-API-Key'

    # OAuth2 fields
    client_id: str | None = None
    client_secret: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: list[str] = pydantic.Field(default_factory=list)
    flow: typing.Literal['authorization_code', 'client_credentials', 'passthrough'] | None = None

    def resolve_header(self) -> str | None:
        """Return ``Bearer <token>`` for ``bearer``, the raw token for ``api_key``, or ``None`` otherwise."""
        token = _resolve_env_var(self.token)

        if not token:
            return None

        if self.type == 'bearer':
            return f'Bearer {token}'
        if self.type == 'api_key':
            return token
        return None

    def resolve_header_name(self) -> str:
        """HTTP header that carries credentials (the configured ``api_key_header`` or ``Authorization``)."""
        if self.type == 'api_key':
            return self.api_key_header
        return 'Authorization'

    def resolve_client_id(self) -> str | None:
        """OAuth client id after env-var substitution."""
        return _resolve_env_var(self.client_id)

    def resolve_client_secret(self) -> str | None:
        """OAuth client secret after env-var substitution."""
        return _resolve_env_var(self.client_secret)


class CORSConfig(pydantic.BaseModel):
    """Starlette CORS middleware settings for the gateway HTTP app."""

    allow_origins: list[str] = ['*']
    allow_methods: list[str] = ['*']
    allow_headers: list[str] = ['*']
    expose_headers: list[str] = ['*']


class StoreConfig(pydantic.BaseModel):
    """Selects the ``TokenStore`` backend (in-process memory or Redis)."""

    type: typing.Literal['memory', 'redis'] = 'memory'
    redis_url: str = 'redis://localhost:6379'
    key_prefix: str = 'mcp_gw'


class LoggingConfig(pydantic.BaseModel):
    """Logging configuration.

    CLI flags (``--log-level``, ``--log-format``, ``--log-file``, ``-v`` / ``-q``) override these.
    """

    level: typing.Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO'
    format: typing.Literal['text', 'json'] = 'text'
    file: str | None = None


class PolicyConfig(pydantic.BaseModel):
    """Glob filters that decide which operations become MCP tools."""

    allow: list[str] | None = None
    deny: list[str] | None = None
    marked_only: bool = False


class ServerConfig(pydantic.BaseModel):
    """One upstream API: OpenAPI location, auth, policy, and HTTP timeout."""

    name: str
    spec: str
    base_url: str | None = None
    path_prefix: str | None = None
    auth: AuthConfig = AuthConfig()
    policy: PolicyConfig = PolicyConfig()
    timeout: float = 90
    exposure: typing.Literal['static', 'dynamic'] = 'static'
    mode: typing.Literal['tool_only', 'auto'] = 'tool_only'
    operations: dict[str, McpIntegration] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator('name')
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v.replace('-', '').replace('_', '').isalnum():
            raise ValueError(f'Server name must be alphanumeric (with - or _): {v}')
        return v

    @pydantic.computed_field
    @property
    def mount_path(self) -> str:
        """HTTP path prefix for this server, derived from ``path_prefix`` or ``name``."""
        if self.path_prefix is not None:
            return f'/{self.path_prefix.strip("/")}'
        return f'/{self.name}'


class GatewayConfig(pydantic.BaseModel):
    """Process-wide gateway settings: listen address, transports, store, logging, and registered servers."""

    host: str = '0.0.0.0'
    port: int = 8000
    url: str = ''
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] = 'streamable-http'
    debug: bool = False
    enable_docs: bool = False
    cors: CORSConfig = CORSConfig()
    store: StoreConfig = StoreConfig()
    logging: LoggingConfig = LoggingConfig()
    servers: list[ServerConfig] = pydantic.Field(default_factory=list)

    @pydantic.model_validator(mode='after')
    def _set_default_url(self) -> typing.Self:
        """Default ``url`` to ``http://{host}:{port}`` when unset, mapping ``0.0.0.0`` to ``localhost``."""
        if not self.url:
            public_host = 'localhost' if self.host == '0.0.0.0' else self.host
            self.url = f'http://{public_host}:{self.port}'
        return self

    @classmethod
    def from_yaml(cls, path: str | pathlib.Path) -> typing.Self:
        """Load gateway config from a YAML file (thin wrapper over ``build_gateway_config``)."""
        return typing.cast(typing.Self, build_gateway_config(yaml_layer(path)))

    @classmethod
    def from_single_spec(
        cls,
        spec: str,
        name: str = 'api',
        base_url: str | None = None,
        auth: AuthConfig | None = None,
        transport: typing.Literal['sse', 'streamable-http', 'stdio'] | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> typing.Self:
        """Build a single-server config; ``host`` / ``port`` / ``transport`` only override when set."""
        layers: list[dict[str, typing.Any]] = [
            single_spec_layer(spec=spec, name=name, base_url=base_url, auth=auth or AuthConfig()),
        ]
        runtime: dict[str, typing.Any] = {}
        if host is not None:
            runtime['host'] = host
        if port is not None:
            runtime['port'] = port
        if transport is not None:
            runtime['transport'] = transport
        if runtime:
            layers.append(runtime)
        return typing.cast(typing.Self, build_gateway_config(*layers))


def yaml_layer(path: str | pathlib.Path) -> dict[str, typing.Any]:
    """Read a YAML config file into a partial-settings dict.

    Empty files yield an empty layer (no contributions).
    Missing files raise ``FileNotFoundError`` to match historical behaviour.
    """
    resolved = pathlib.Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f'Config file not found: {resolved}')
    raw = yaml.safe_load(resolved.read_text(encoding='utf-8'))
    return raw or {}


def single_spec_layer(
    *,
    spec: str,
    name: str = 'api',
    base_url: str | None = None,
    auth: AuthConfig | None = None,
) -> dict[str, typing.Any]:
    """Build a partial-settings dict for the single-spec CLI / API entry path.

    The returned dict mirrors the ``servers:`` shape that ``GatewayConfig`` expects,
    so it composes via ``build_gateway_config`` like any other layer.
    """
    server: dict[str, typing.Any] = {'name': name, 'spec': spec}
    if base_url is not None:
        server['base_url'] = base_url
    server['auth'] = (auth or AuthConfig()).model_dump()
    return {'servers': [server]}


def build_gateway_config(*layers: dict[str, typing.Any]) -> GatewayConfig:
    """Compose layered partial-settings dicts (later wins) into a validated ``GatewayConfig``.

    The contract for a *layer* is: a dict containing only the fields that source
    actually supplies. Pydantic defaults form the implicit floor, so callers
    never need to repeat them. Order encodes precedence; typical CLI use is::

        build_gateway_config(yaml_layer, cli_layer)

    Sub-trees (``logging``, per-server ``policy`` / ``auth``) merge recursively,
    so a CLI flag that only sets ``logging.level`` does not erase ``logging.format`` from YAML.
    Lists (``servers``) are replaced wholesale, since merging by index is rarely what you want.
    """
    merged: dict[str, typing.Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    return GatewayConfig.model_validate(merged)
