import os
import pathlib
import re
import typing

import pydantic
import yaml


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
    """

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
        """Load gateway config from a YAML file."""
        path = pathlib.Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f'Config file not found: {path}')
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        return cls.model_validate(raw)

    @classmethod
    def from_single_spec(
        cls,
        spec: str,
        name: str = 'api',
        base_url: str | None = None,
        auth: AuthConfig | None = None,
        transport: typing.Literal['sse', 'streamable-http', 'stdio'] = 'streamable-http',
        host: str = '0.0.0.0',
        port: int = 8000,
    ) -> typing.Self:
        """Build a single-server config (CLI convenience wrapper)."""
        entry = ServerConfig(name=name, spec=spec, base_url=base_url, auth=auth or AuthConfig())
        return cls(
            host=host,
            port=port,
            transport=transport,
            servers=[entry],
        )
