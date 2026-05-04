import pathlib

import pytest
import yaml

from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
)


EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[2] / 'examples'


class TestServerConfig:
    """Mount path derivation and name validation on ``ServerConfig``."""

    def test_mount_path_default(self):
        """Mount path defaults to ``/<name>`` when no prefix is set."""
        server = ServerConfig(name='petstore', spec='petstore.json')
        assert server.mount_path == '/petstore'

    def test_mount_path_custom(self):
        """``path_prefix`` overrides the name-based default."""
        server = ServerConfig(name='petstore', spec='petstore.json', path_prefix='pets')
        assert server.mount_path == '/pets'

    def test_mount_path_strips_slashes(self):
        """Surrounding slashes on ``path_prefix`` are normalised away."""
        server = ServerConfig(name='petstore', spec='petstore.json', path_prefix='/pets/')
        assert server.mount_path == '/pets'

    def test_name_validation_valid(self):
        """Alphanumerics, dashes and underscores are accepted in ``name``."""
        ServerConfig(name='my-api', spec='x.json')
        ServerConfig(name='my_api', spec='x.json')
        ServerConfig(name='api123', spec='x.json')

    def test_name_validation_invalid(self):
        """Spaces or punctuation in ``name`` raise ``ValueError``."""
        with pytest.raises(ValueError, match='alphanumeric'):
            ServerConfig(name='my api!', spec='x.json')


class TestAuthConfig:
    """Header resolution and env-var substitution on ``AuthConfig``."""

    @pytest.mark.parametrize(
        ('auth_type', 'token', 'expected'),
        [
            ('bearer', 'my-token', 'Bearer my-token'),
            ('api_key', 'key123', 'key123'),
        ],
    )
    def test_resolve_header_static(self, auth_type, token, expected):
        """Static tokens render as ``Bearer …`` for bearer and as-is for api_key."""
        auth = AuthConfig(type=auth_type, token=token)
        assert auth.resolve_header() == expected

    def test_resolve_api_key_default_header_name(self):
        """Default api_key header name is ``X-API-Key``."""
        auth = AuthConfig(type='api_key', token='key123')
        assert auth.resolve_header_name() == 'X-API-Key'

    def test_resolve_api_key_custom_header(self):
        """``api_key_header`` overrides the default header name."""
        auth = AuthConfig(type='api_key', token='key123', api_key_header='X-Custom')
        assert auth.resolve_header_name() == 'X-Custom'

    def test_resolve_bearer_from_env(self, monkeypatch):
        """``${VAR}`` placeholders are resolved against the environment."""
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        auth = AuthConfig(type='bearer', token='${TEST_TOKEN}')
        assert auth.resolve_header() == 'Bearer env-token'

    def test_resolve_env_var_with_default(self):
        """``${VAR:-default}`` falls back to the default when the var is unset."""
        auth = AuthConfig(type='bearer', token='${NONEXISTENT_VAR:-fallback-token}')
        assert auth.resolve_header() == 'Bearer fallback-token'

    def test_resolve_env_var_unset_no_default(self):
        """An unset env var with no default resolves to ``None``."""
        auth = AuthConfig(type='bearer', token='${NONEXISTENT_VAR}')
        assert auth.resolve_header() is None

    def test_resolve_no_token(self):
        """A bearer config without a token resolves to ``None``."""
        auth = AuthConfig(type='bearer')
        assert auth.resolve_header() is None

    def test_resolve_none_type(self):
        """``type='none'`` ignores any token and resolves to ``None``."""
        auth = AuthConfig(type='none', token='ignored')
        assert auth.resolve_header() is None

    def test_resolve_client_id(self, monkeypatch):
        """OAuth ``client_id`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('CID', 'client-123')
        auth = AuthConfig(type='oauth2', client_id='${CID}')
        assert auth.resolve_client_id() == 'client-123'

    def test_resolve_client_id_direct(self):
        """A literal ``client_id`` is returned verbatim."""
        auth = AuthConfig(type='oauth2', client_id='direct-id')
        assert auth.resolve_client_id() == 'direct-id'

    def test_resolve_client_secret(self, monkeypatch):
        """OAuth ``client_secret`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('SEC', 'secret-456')
        auth = AuthConfig(type='oauth2', client_secret='${SEC}')
        assert auth.resolve_client_secret() == 'secret-456'


class TestGatewayConfig:
    """Top-level gateway config: defaults, YAML loading, single-spec builder."""

    def test_default_url(self):
        """Default public URL is ``http://localhost:8000``."""
        config = GatewayConfig()
        assert config.url == 'http://localhost:8000'

    def test_custom_url(self):
        """Explicit ``url`` is preserved."""
        config = GatewayConfig(url='https://mcp.example.com')
        assert config.url == 'https://mcp.example.com'

    def test_from_yaml(self, tmp_path):
        """``from_yaml`` loads host/port/servers from a YAML file."""
        data = {
            'host': '127.0.0.1',
            'port': 9000,
            'servers': [
                {'name': 'test', 'spec': 'test.json'},
            ],
        }
        yaml_path = tmp_path / 'config.yml'
        yaml_path.write_text(yaml.dump(data))
        config = GatewayConfig.from_yaml(yaml_path)
        assert config.host == '127.0.0.1'
        assert config.port == 9000
        assert len(config.servers) == 1
        assert config.servers[0].name == 'test'

    def test_from_yaml_not_found(self):
        """Missing YAML files surface as ``FileNotFoundError``."""
        with pytest.raises(FileNotFoundError):
            GatewayConfig.from_yaml('/nonexistent/path.yml')

    def test_from_single_spec(self):
        """``from_single_spec`` produces a one-server config with overridden port."""
        config = GatewayConfig.from_single_spec('petstore.json', name='pets', port=3000)
        assert len(config.servers) == 1
        assert config.servers[0].name == 'pets'
        assert config.servers[0].spec == 'petstore.json'
        assert config.port == 3000

    def test_store_config_default(self):
        """Default token store is in-memory."""
        config = GatewayConfig()
        assert config.store.type == 'memory'

    def test_store_config_redis(self, tmp_path):
        """``store: redis`` block in YAML hydrates url and key prefix."""
        data = {
            'store': {
                'type': 'redis',
                'redis_url': 'redis://redis:6379',
                'key_prefix': 'custom',
            },
            'servers': [],
        }
        yaml_path = tmp_path / 'config.yml'
        yaml_path.write_text(yaml.dump(data))
        config = GatewayConfig.from_yaml(yaml_path)
        assert config.store.type == 'redis'
        assert config.store.redis_url == 'redis://redis:6379'
        assert config.store.key_prefix == 'custom'


@pytest.mark.parametrize('yaml_path', sorted(EXAMPLES_DIR.glob('*.yml')))
def test_example_yaml_parses(yaml_path: pathlib.Path):
    """Every shipped example YAML must parse into a valid GatewayConfig."""
    config = GatewayConfig.from_yaml(yaml_path)
    assert config.servers, f'{yaml_path.name} declares no servers'
    for server in config.servers:
        assert server.name
        assert server.spec
