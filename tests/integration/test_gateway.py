import json
import pathlib
import typing
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp.server.fastmcp import Context
from starlette.testclient import TestClient

from openapi_mcp_gateway.auth import token_source as token_source_module
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

        assert result.isError is False
        assert json.loads(result.content[0].text) == [{'id': 1, 'name': 'fido'}]


class TestDynamicExposureEndToEnd:
    """Full assembly chain when ``exposure: dynamic`` swaps tools for the three meta-tools."""

    @pytest.fixture
    def dynamic_gateway(self, petstore_json_path):
        """Petstore gateway with the server flipped to dynamic exposure."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='petstore', spec=str(petstore_json_path), exposure='dynamic'),
            ],
        )
        return Gateway.from_config(config)

    def test_only_three_meta_tools_registered(self, dynamic_gateway):
        """An MCP client sees just ``list_operations``, ``get_operation``, ``call_operation``."""
        mcp = dynamic_gateway._servers[0].mcp
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert names == {'list_operations', 'get_operation', 'call_operation'}

    async def test_list_then_get_then_call_roundtrip(self, dynamic_gateway, mock_upstream):
        """An LLM-style ``list → get → call`` sequence drives an upstream call with correct query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        mcp = dynamic_gateway._servers[0].mcp
        tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

        listing = json.loads(await tools['list_operations'].fn(ctx=_stub_context()))
        assert {entry['name'] for entry in listing} >= {'list_pets', 'get_pet_by_id'}

        described = json.loads(await tools['get_operation'].fn(name='list_pets', ctx=_stub_context()))
        assert described['input_schema']['properties']['limit']['type'] == 'integer'

        result = await tools['call_operation'].fn(name='list_pets', arguments={'limit': 5}, ctx=_stub_context())
        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert result.isError is False
        assert json.loads(result.content[0].text) == [{'id': 1, 'name': 'fido'}]


def _write_client_credentials_spec(tmp_path: pathlib.Path) -> pathlib.Path:
    """Persist a minimal OpenAPI spec declaring only ``clientCredentials`` security."""
    spec = {
        'openapi': '3.0.0',
        'info': {'title': 'cc-petstore', 'version': '1.0.0'},
        'servers': [{'url': 'https://petstore.example.com/v1'}],
        'components': {
            'securitySchemes': {
                'oauth2': {
                    'type': 'oauth2',
                    'flows': {
                        'clientCredentials': {
                            'tokenUrl': 'https://auth.example.com/token',
                            'scopes': {'api': 'API access'},
                        },
                    },
                },
            },
        },
        'paths': {
            '/pets': {
                'get': {
                    'operationId': 'listPets',
                    'summary': 'List pets',
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }
    path = tmp_path / 'cc-petstore.json'
    path.write_text(json.dumps(spec), encoding='utf-8')
    return path


class TestClientCredentialsFlowEndToEnd:
    """End-to-end behaviour of the ``client_credentials`` OAuth flow."""

    @pytest.fixture
    def cc_gateway_config(self, tmp_path):
        """Config for a single-server gateway whose spec declares only clientCredentials."""
        spec_path = _write_client_credentials_spec(tmp_path)
        return GatewayConfig(
            servers=[
                ServerConfig(
                    name='petstore',
                    spec=str(spec_path),
                    auth=AuthConfig(
                        type='oauth2',
                        client_id='gateway-id',
                        client_secret='gateway-secret',
                        scopes=['api'],
                    ),
                ),
            ],
        )

    def test_setup_uses_client_credentials_flow(self, cc_gateway_config):
        """The gateway picks the client_credentials flow when only that flow is declared."""
        gateway = Gateway.from_config(cc_gateway_config)
        bundle = gateway._servers[0]
        assert bundle.auth_provider is None
        assert bundle.auth_settings is None
        assert len(gateway._shutdown_hooks) == 1

    async def test_tool_call_attaches_fetched_bearer(self, cc_gateway_config, mock_upstream, monkeypatch):
        """A tool call fetches a token from the IdP, then forwards it as ``Authorization`` upstream."""
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'access_token': 'cc-bearer-xyz', 'expires_in': 3600}
        token_response.text = ''

        token_post_mock = AsyncMock(return_value=token_response)
        monkeypatch.setattr(
            token_source_module.httpx.AsyncClient,
            'post',
            token_post_mock,
            raising=False,
        )

        gateway = Gateway.from_config(cc_gateway_config)

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['authorization'] = request.headers.get('authorization')
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        mcp = gateway._servers[0].mcp
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'list_pets')
        await tool.run({}, context=_stub_context())

        assert captured['authorization'] == 'Bearer cc-bearer-xyz'
        token_post_mock.assert_awaited_once()
        post_args = token_post_mock.await_args
        assert post_args is not None
        # The first positional arg is the URL, second positional or kwargs carry data.
        assert (
            post_args.args[0] == 'https://auth.example.com/token'
            or post_args.kwargs.get('url') == 'https://auth.example.com/token'
        )
        assert post_args.kwargs['data']['grant_type'] == 'client_credentials'

    async def test_token_is_cached_across_tool_calls(self, cc_gateway_config, mock_upstream, monkeypatch):
        """Multiple tool calls share a single cached token (one POST to the IdP)."""
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'access_token': 'cached', 'expires_in': 3600}
        token_response.text = ''

        token_post_mock = AsyncMock(return_value=token_response)
        monkeypatch.setattr(
            token_source_module.httpx.AsyncClient,
            'post',
            token_post_mock,
            raising=False,
        )

        gateway = Gateway.from_config(cc_gateway_config)
        mock_upstream(lambda request: httpx.Response(200, json=[]))

        mcp = gateway._servers[0].mcp
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'list_pets')
        await tool.run({}, context=_stub_context())
        await tool.run({}, context=_stub_context())
        await tool.run({}, context=_stub_context())

        assert token_post_mock.await_count == 1
