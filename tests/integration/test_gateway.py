"""Integration tests for Gateway assembly and HTTP endpoints."""

import pytest
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import AuthConfig, GatewayConfig, PolicyConfig, ServerConfig


@pytest.fixture
def gateway(petstore_json_path):
    config = GatewayConfig(
        servers=[
            ServerConfig(name='petstore', spec=str(petstore_json_path)),
        ],
    )
    return Gateway.from_config(config)


@pytest.fixture
def app(gateway):
    return gateway._build_app(transport='streamable-http')


@pytest.fixture
def client(app):
    return TestClient(app)


class TestGatewayAssembly:
    def test_servers_registered(self, gateway):
        assert len(gateway._servers) == 1
        assert gateway._servers[0].name == 'petstore'
        assert gateway._servers[0].mount_path == '/petstore'

    def test_spec_parsed(self, gateway):
        spec = gateway._servers[0].spec
        assert spec.title == 'Petstore'
        ids = [op.operation_id for op in spec.operations]
        assert 'listPets' in ids

    def test_no_auth_provider_for_no_auth(self, gateway):
        assert gateway._servers[0].auth_provider is None

    def test_multiple_servers(self, petstore_json_path):
        config = GatewayConfig(
            servers=[
                ServerConfig(name='pets', spec=str(petstore_json_path)),
                ServerConfig(name='pets2', spec=str(petstore_json_path), path_prefix='other'),
            ],
        )
        gw = Gateway.from_config(config)
        assert len(gw._servers) == 2
        paths = [s.mount_path for s in gw._servers]
        assert '/pets' in paths
        assert '/other' in paths

    def test_empty_operations_raises(self, petstore_json_path):
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='test',
                    spec=str(petstore_json_path),
                    policy=PolicyConfig(allow=['NONEXISTENT_OPERATION']),
                ),
            ],
        )
        with pytest.raises(ValueError, match='no operations to expose'):
            Gateway.from_config(config)


class TestHealthEndpoint:
    def test_healthz(self, client):
        response = client.get('/healthz')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        assert len(data['servers']) == 1
        assert data['servers'][0]['name'] == 'petstore'
        assert data['servers'][0]['auth'] == 'static'


class TestWellKnownEndpoints:
    def test_no_auth_server_returns_404(self, client):
        response = client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 404

    def test_no_auth_protected_resource_returns_404(self, client):
        response = client.get('/.well-known/oauth-protected-resource/petstore')
        assert response.status_code == 404

    def test_unknown_server_404(self, client):
        response = client.get('/.well-known/oauth-authorization-server/unknown')
        assert response.status_code == 404


class TestWellKnownOAuth:
    """Well-known endpoints for an OAuth-enabled server."""

    @pytest.fixture
    def oauth_client(self, petstore_json_path):
        config = GatewayConfig(
            url='https://mcp.example.com',
            servers=[
                ServerConfig(
                    name='petstore',
                    spec=str(petstore_json_path),
                    auth=AuthConfig(
                        type='oauth2',
                        client_id='test-client-id',
                        client_secret='test-client-secret',
                        authorization_url='https://auth.example.com/authorize',
                        token_url='https://auth.example.com/token',
                        scopes=['read'],
                    ),
                ),
            ],
        )
        gw = Gateway.from_config(config)
        app = gw._build_app(transport='streamable-http')
        return TestClient(app)

    def test_authorization_server_metadata(self, oauth_client):
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        data = response.json()
        assert 'petstore' in data['issuer']
        assert data['authorization_endpoint'].endswith('/authorize')
        assert data['token_endpoint'].endswith('/token')
        assert 'S256' in data['code_challenge_methods_supported']

    def test_authorization_server_with_mcp(self, oauth_client):
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore/mcp')
        assert response.status_code == 200

    def test_protected_resource_metadata(self, oauth_client):
        response = oauth_client.get('/.well-known/oauth-protected-resource/petstore')
        assert response.status_code == 200
        data = response.json()
        assert data['resource'].endswith('/mcp')
        assert len(data['authorization_servers']) == 1

    def test_options_cors(self, oauth_client):
        response = oauth_client.options('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
