import os
import pathlib
import re
import typing

import pydantic
import yaml


def _resolve_env_var(value: str | None) -> str | None:
    """Resolve ``${ENV_VAR}`` or ``${ENV_VAR:-default}`` in a string value.

    If the entire string is a single ``${...}`` reference, returns the resolved
    value (or None when the env var is unset and no default is given).
    Otherwise returns the original string unchanged.
    """
    if value is None:
        return None
    m = re.fullmatch(r'\$\{(\w+)(?::-(.*))?\}', value)
    if not m:
        return value
    env_value = os.environ.get(m.group(1))
    if env_value is not None:
        return env_value
    if m.group(2) is not None:
        return m.group(2)
    return None


class AuthConfig(pydantic.BaseModel):
    """Authentication configuration for an upstream API.

    String fields (token, client_id, client_secret) support ``${ENV_VAR}``
    and ``${ENV_VAR:-default}`` syntax for reading values from environment
    variables at resolve time.
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

    def resolve_header(self) -> str | None:
        """Resolve the Authorization header value."""
        token = _resolve_env_var(self.token)

        if not token:
            return None

        if self.type == 'bearer':
            return f'Bearer {token}'
        if self.type == 'api_key':
            return token
        return None

    def resolve_header_name(self) -> str:
        if self.type == 'api_key':
            return self.api_key_header
        return 'Authorization'

    def resolve_client_id(self) -> str | None:
        return _resolve_env_var(self.client_id)

    def resolve_client_secret(self) -> str | None:
        return _resolve_env_var(self.client_secret)


class CORSConfig(pydantic.BaseModel):
    """CORS middleware configuration."""

    allow_origins: list[str] = ['*']
    allow_methods: list[str] = ['*']
    allow_headers: list[str] = ['*']
    expose_headers: list[str] = ['*']


class StoreConfig(pydantic.BaseModel):
    """Token store backend configuration."""

    type: typing.Literal['memory', 'redis'] = 'memory'
    redis_url: str = 'redis://localhost:6379'
    key_prefix: str = 'mcp_gw'


class PolicyConfig(pydantic.BaseModel):
    """Policy rules for filtering operations."""

    allow: list[str] | None = None
    deny: list[str] | None = None
    marked_only: bool = False


class ServerConfig(pydantic.BaseModel):
    """Configuration for a single API server."""

    name: str
    spec: str  # File path or URL to OpenAPI spec
    base_url: str | None = None  # Override the base URL from spec
    path_prefix: str | None = None  # Override the mount path (default: /{name})
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
        """The URL path where this server is mounted."""
        if self.path_prefix is not None:
            return f'/{self.path_prefix.strip("/")}'
        return f'/{self.name}'


class GatewayConfig(pydantic.BaseModel):
    """Top-level gateway configuration."""

    host: str = '0.0.0.0'
    port: int = 8000
    url: str = ''
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] = 'streamable-http'
    debug: bool = False
    enable_docs: bool = False
    cors: CORSConfig = CORSConfig()
    store: StoreConfig = StoreConfig()
    servers: list[ServerConfig] = pydantic.Field(default_factory=list)

    @pydantic.model_validator(mode='after')
    def _set_default_url(self) -> typing.Self:
        if not self.url:
            public_host = 'localhost' if self.host == '0.0.0.0' else self.host
            self.url = f'http://{public_host}:{self.port}'
        return self

    @classmethod
    def from_yaml(cls, path: str | pathlib.Path) -> 'GatewayConfig':
        """Load configuration from a YAML file."""
        p = pathlib.Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f'Config file not found: {p}')
        raw = yaml.safe_load(p.read_text(encoding='utf-8'))
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
    ) -> 'GatewayConfig':
        """Create a config for a single OpenAPI spec (CLI shorthand)."""
        entry = ServerConfig(name=name, spec=spec, base_url=base_url, auth=auth or AuthConfig())
        return cls(
            host=host,
            port=port,
            transport=transport,
            servers=[entry],
        )
