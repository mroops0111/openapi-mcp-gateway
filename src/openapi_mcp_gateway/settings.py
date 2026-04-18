"""Configuration models for the gateway."""

import os
import pathlib
import typing

import pydantic
import yaml


class AuthConfig(pydantic.BaseModel):
    """Authentication configuration for an upstream API."""

    type: typing.Literal['bearer', 'api_key', 'oauth2', 'none'] = 'none'
    token: str | None = None
    token_env: str | None = None
    api_key_header: str = 'X-API-Key'

    # OAuth2 fields
    client_id: str | None = None
    client_id_env: str | None = None
    client_secret: str | None = None
    client_secret_env: str | None = None
    scopes: list[str] = pydantic.Field(default_factory=list)

    def resolve_header(self) -> str | None:
        """Resolve the Authorization header value."""
        token = self.token
        if not token and self.token_env:
            token = os.environ.get(self.token_env)

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
        return self.client_id or (os.environ.get(self.client_id_env) if self.client_id_env else None)

    def resolve_client_secret(self) -> str | None:
        return self.client_secret or (os.environ.get(self.client_secret_env) if self.client_secret_env else None)


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
            self.url = f'http://{self.host}:{self.port}'
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
        transport: str = 'streamable-http',
        host: str = '0.0.0.0',
        port: int = 8000,
    ) -> 'GatewayConfig':
        """Create a config for a single OpenAPI spec (CLI shorthand)."""
        entry = ServerConfig(name=name, spec=spec, base_url=base_url)
        return cls(
            host=host,
            port=port,
            transport=transport,
            servers=[entry],
        )
