import json
import typing

import httpx
import pytest
from mcp.server.fastmcp import Context
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import AuthConfig, GatewayConfig, PolicyConfig, ServerConfig


class _StubContext:
    """No-op MCP context used when invoking generated tools end-to-end."""

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context() -> Context:
    """Return a ``Context``-typed stub suitable for tool invocation."""
    return typing.cast(Context, _StubContext())


@pytest.fixture
def gateway(petstore_json_path):
    """Single-server petstore gateway with no upstream auth configured."""
    config = GatewayConfig(
        servers=[
            ServerConfig(name='petstore', spec=str(petstore_json_path)),
        ],
    )
    return Gateway.from_config(config)


@pytest.fixture
def app(gateway):
    """Starlette app built from the no-auth gateway over streamable-http."""
    return gateway._build_app(transport='streamable-http')


@pytest.fixture
def client(app):
    """Test client over the no-auth gateway app."""
    return TestClient(app)


class TestGatewayAssembly:
    """Server registration, spec parsing, auth provider wiring, and policy validation."""

    def test_servers_registered(self, gateway):
        """A configured server appears in ``_servers`` with its derived mount path."""
        assert len(gateway._servers) == 1
        assert gateway._servers[0].name == 'petstore'
        assert gateway._servers[0].mount_path == '/petstore'

    def test_spec_parsed(self, gateway):
        """The OpenAPI spec is parsed and its operations are discoverable."""
        spec = gateway._servers[0].spec
        assert spec.title == 'Petstore'
        ids = [op.operation_id for op in spec.operations]
        assert 'listPets' in ids

    def test_no_auth_provider_for_no_auth(self, gateway):
        """A server without auth config has no ``auth_provider`` attached."""
        assert gateway._servers[0].auth_provider is None

    def test_multiple_servers(self, petstore_json_path):
        """Multiple servers register on independent mount paths."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='pets', spec=str(petstore_json_path)),
                ServerConfig(name='pets2', spec=str(petstore_json_path), path_prefix='other'),
            ],
        )
        gateway = Gateway.from_config(config)
        assert len(gateway._servers) == 2
        paths = [server.mount_path for server in gateway._servers]
        assert '/pets' in paths
        assert '/other' in paths

    def test_empty_operations_raises(self, petstore_json_path):
        """A policy that filters every operation out fails fast at assembly time."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='test',
                    spec=str(petstore_json_path),
                    policy=PolicyConfig(allow=['NONEXISTENT_OPERATION']),
                ),
            ],
        )
        with pytest.raises(ValueError):
            Gateway.from_config(config)


class TestHealthEndpoint:
    """``/healthz`` reports overall status and per-server auth mode."""

    def test_healthz(self, client):
        """Endpoint reports ``status: ok`` and lists every registered server."""
        response = client.get('/healthz')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        assert len(data['servers']) == 1
        assert data['servers'][0]['name'] == 'petstore'
        assert data['servers'][0]['auth'] == 'static'


class TestWellKnownNoOAuth:
    """Well-known endpoints return 404 when the server has no OAuth or is unknown."""

    @pytest.mark.parametrize(
        'path',
        [
            '/.well-known/oauth-authorization-server/petstore',
            '/.well-known/oauth-protected-resource/petstore',
            '/.well-known/oauth-authorization-server/unknown',
        ],
    )
    def test_returns_404(self, client, path):
        """Both endpoint variants and unknown server names yield 404."""
        response = client.get(path)
        assert response.status_code == 404


class TestWellKnownOAuth:
    """Well-known endpoints for an OAuth-enabled server."""

    @pytest.fixture
    def oauth_client(self, petstore_json_path):
        """Test client over a gateway whose petstore server uses OAuth2."""
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
        gateway = Gateway.from_config(config)
        app = gateway._build_app(transport='streamable-http')
        return TestClient(app)

    def test_authorization_server_metadata(self, oauth_client):
        """OAuth metadata document advertises issuer, endpoints and PKCE method."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        data = response.json()
        assert 'petstore' in data['issuer']
        assert data['authorization_endpoint'].endswith('/authorize')
        assert data['token_endpoint'].endswith('/token')
        assert 'S256' in data['code_challenge_methods_supported']

    def test_authorization_server_with_mcp(self, oauth_client):
        """Suffixing ``/mcp`` on the metadata path is also served."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore/mcp')
        assert response.status_code == 200

    def test_protected_resource_metadata(self, oauth_client):
        """Protected-resource metadata points to the MCP endpoint and authorization servers."""
        response = oauth_client.get('/.well-known/oauth-protected-resource/petstore')
        assert response.status_code == 200
        data = response.json()
        assert data['resource'].endswith('/mcp')
        assert len(data['authorization_servers']) == 1

    def test_options_cors(self, oauth_client):
        """``OPTIONS`` on the metadata endpoint returns 200 for CORS preflight."""
        response = oauth_client.options('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200


class TestEndToEndToolInvocation:
    """Full assembly chain: spec → operations → tool registration → upstream HTTP call."""

    async def test_list_pets_calls_upstream_with_query_params(self, gateway, mock_upstream):
        """Invoking the generated ``listPets`` tool reaches the upstream URL with the right query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        mcp = gateway._servers[0].mcp
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'list_pets')
        result = await tool.run({'limit': 5}, context=_stub_context())

        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert captured['url'].startswith('https://petstore.example.com/v1/pets')

        assert json.loads(result) == [{'id': 1, 'name': 'fido'}]
