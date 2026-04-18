"""Tests for configuration models."""

import pytest
import yaml

from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
)


class TestServerConfig:
    def test_mount_path_default(self):
        s = ServerConfig(name='petstore', spec='petstore.json')
        assert s.mount_path == '/petstore'

    def test_mount_path_custom(self):
        s = ServerConfig(name='petstore', spec='petstore.json', path_prefix='pets')
        assert s.mount_path == '/pets'

    def test_mount_path_strips_slashes(self):
        s = ServerConfig(name='petstore', spec='petstore.json', path_prefix='/pets/')
        assert s.mount_path == '/pets'

    def test_name_validation_valid(self):
        ServerConfig(name='my-api', spec='x.json')
        ServerConfig(name='my_api', spec='x.json')
        ServerConfig(name='api123', spec='x.json')

    def test_name_validation_invalid(self):
        with pytest.raises(ValueError, match='alphanumeric'):
            ServerConfig(name='my api!', spec='x.json')


class TestAuthConfig:
    def test_resolve_bearer(self):
        auth = AuthConfig(type='bearer', token='my-token')
        assert auth.resolve_header() == 'Bearer my-token'

    def test_resolve_api_key(self):
        auth = AuthConfig(type='api_key', token='key123')
        assert auth.resolve_header() == 'key123'
        assert auth.resolve_header_name() == 'X-API-Key'

    def test_resolve_api_key_custom_header(self):
        auth = AuthConfig(type='api_key', token='key123', api_key_header='X-Custom')
        assert auth.resolve_header_name() == 'X-Custom'

    def test_resolve_bearer_from_env(self, monkeypatch):
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        auth = AuthConfig(type='bearer', token_env='TEST_TOKEN')
        assert auth.resolve_header() == 'Bearer env-token'

    def test_resolve_no_token(self):
        auth = AuthConfig(type='bearer')
        assert auth.resolve_header() is None

    def test_resolve_none_type(self):
        auth = AuthConfig(type='none', token='ignored')
        assert auth.resolve_header() is None

    def test_resolve_client_id(self, monkeypatch):
        monkeypatch.setenv('CID', 'client-123')
        auth = AuthConfig(type='oauth2', client_id_env='CID')
        assert auth.resolve_client_id() == 'client-123'

    def test_resolve_client_id_direct(self):
        auth = AuthConfig(type='oauth2', client_id='direct-id')
        assert auth.resolve_client_id() == 'direct-id'

    def test_resolve_client_secret(self, monkeypatch):
        monkeypatch.setenv('SEC', 'secret-456')
        auth = AuthConfig(type='oauth2', client_secret_env='SEC')
        assert auth.resolve_client_secret() == 'secret-456'


class TestGatewayConfig:
    def test_default_url(self):
        config = GatewayConfig()
        assert config.url == 'http://0.0.0.0:8000'

    def test_custom_url(self):
        config = GatewayConfig(url='https://mcp.example.com')
        assert config.url == 'https://mcp.example.com'

    def test_from_yaml(self, tmp_path):
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
        with pytest.raises(FileNotFoundError):
            GatewayConfig.from_yaml('/nonexistent/path.yml')

    def test_from_single_spec(self):
        config = GatewayConfig.from_single_spec('petstore.json', name='pets', port=3000)
        assert len(config.servers) == 1
        assert config.servers[0].name == 'pets'
        assert config.servers[0].spec == 'petstore.json'
        assert config.port == 3000

    def test_store_config_default(self):
        config = GatewayConfig()
        assert config.store.type == 'memory'

    def test_store_config_redis(self, tmp_path):
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
