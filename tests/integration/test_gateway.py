import contextlib
import json
import pathlib
import socket
import sys
import time
import typing
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.server.mcpserver import Context
from mcp.types import RequestParamsMeta, TextContent
from starlette.testclient import TestClient

from openapi_mcp_gateway.app import _transport_security
from openapi_mcp_gateway.auth import token_source as token_source_module
from openapi_mcp_gateway.auth.oidc import IssuerMetadata
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    DNSRebindingProtectionConfig,
    ExposureConfig,
    GatewayConfig,
    PolicyConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from tests.constants import (
    API_URL,
    ATTACKER_HOST,
    ATTACKER_URL,
    AUTHORIZE_URL,
    BROWSER_ORIGIN,
    GATEWAY_HOST,
    GATEWAY_URL,
    ISSUER,
    JWKS_URL,
    PETSTORE_URL,
    TOKEN_URL,
)


class _StubContext:
    """No-op MCP context used when invoking generated tools end-to-end."""

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context() -> Context:
    """Return a ``Context``-typed stub suitable for tool invocation."""
    return typing.cast(Context, _StubContext())


def _tool(gateway: Gateway, name: str):
    """The registered tool called ``name`` on the gateway's only server."""
    return next(tool for tool in gateway._servers[0].mcp._tool_manager.list_tools() if tool.name == name)


def _delegating_client(spec_path: pathlib.Path, jwk_client: MagicMock | None = None) -> TestClient:
    """Test client over a petstore server that validates tokens from ``ISSUER``, an issuer it does not own."""
    metadata = IssuerMetadata(issuer=ISSUER, jwks_uri=JWKS_URL, token_endpoint=TOKEN_URL)
    config = GatewayConfig(
        url=GATEWAY_URL,
        servers=[
            ServerConfig(
                name='petstore',
                spec=str(spec_path),
                auth=AuthConfig(
                    type='oauth2',
                    flow='token_exchange',
                    issuer=ISSUER,
                    upstream=UpstreamAuthConfig(
                        audience=API_URL, client_id='gateway', client_secret='secret', scopes=['read']
                    ),
                ),
            ),
        ],
    )
    with (
        patch('openapi_mcp_gateway.auth.flows.token_exchange.fetch_issuer_metadata', return_value=metadata),
        patch('openapi_mcp_gateway.auth.oidc._build_jwk_client', return_value=jwk_client or MagicMock()),
    ):
        gateway = Gateway.from_config(config)
        return TestClient(gateway._build_app(transport='streamable-http'))


def _initialize_status(test_client: TestClient, host: str, origin: str | None = None) -> int:
    """Status of an MCP ``initialize`` POST to the petstore endpoint, sent with ``host`` and ``origin``."""
    headers = {'Host': host, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    if origin is not None:
        headers['Origin'] = origin
    body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'initialize',
        'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}},
    }
    return test_client.post('/petstore/mcp', headers=headers, json=body).status_code


@contextlib.asynccontextmanager
async def _serve(app: FastAPI) -> typing.AsyncIterator[str]:
    """Serve ``app`` with uvicorn on a free loopback port and yield its base URL."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, ws='none'))
    served = anyio.Event()

    async def serve() -> None:
        # ``serve`` returns rather than raising when the lifespan fails, so record that it finished.
        await server.serve([sock])
        served.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(serve)
        with anyio.fail_after(5):
            while not server.started and not served.is_set():
                await anyio.sleep(0.01)
        assert server.started, 'the app failed to start'
        try:
            yield f'http://127.0.0.1:{sock.getsockname()[1]}'
        finally:
            server.should_exit = True


@contextlib.asynccontextmanager
async def _connect(gateway: Gateway, spec_path: pathlib.Path, transport: str) -> typing.AsyncIterator[Client]:
    """Connect an MCP client over ``transport``: stdio through the CLI, the HTTP transports through ``gateway``'s app."""
    if transport == 'stdio':
        cli_args = ['--spec', str(spec_path), '--name', 'petstore', '--transport', 'stdio']
        async with Client(
            StdioServerParameters(command=sys.executable, args=['-m', 'openapi_mcp_gateway.cli', *cli_args])
        ) as mcp_client:
            yield mcp_client
        return
    async with _serve(gateway._build_app(transport=transport)) as base_url:
        target = (
            f'{base_url}/petstore/mcp' if transport == 'streamable-http' else sse_client(f'{base_url}/petstore/sse')
        )
        async with Client(target) as mcp_client:
            yield mcp_client


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


@pytest.fixture
def oauth_gateway(petstore_json_path):
    """Gateway whose petstore server uses the OAuth2 authorization-code flow."""
    config = GatewayConfig(
        url=GATEWAY_URL,
        servers=[
            ServerConfig(
                name='petstore',
                spec=str(petstore_json_path),
                auth=AuthConfig(
                    type='oauth2',
                    upstream=UpstreamAuthConfig(
                        client_id='test-client-id',
                        client_secret='test-client-secret',
                        authorization_url=AUTHORIZE_URL,
                        token_url=TOKEN_URL,
                        scopes=['read'],
                    ),
                ),
            ),
        ],
    )
    return Gateway.from_config(config)


@pytest.fixture(scope='module')
def signing_key():
    """One RSA keypair for the module, since generation dominates the runtime."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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
    def oauth_client(self, oauth_gateway):
        """Test client over the OAuth2 gateway."""
        return TestClient(oauth_gateway._build_app(transport='streamable-http'))

    def test_authorization_server_metadata(self, oauth_client):
        """OAuth metadata document advertises issuer, endpoints and PKCE method."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        data = response.json()
        assert 'petstore' in data['issuer']
        assert data['authorization_endpoint'].endswith('/authorize')
        assert data['token_endpoint'].endswith('/token')
        assert 'S256' in data['code_challenge_methods_supported']

    @pytest.mark.parametrize(
        'path',
        ['/.well-known/oauth-authorization-server/petstore', '/petstore/.well-known/oauth-authorization-server'],
    )
    def test_metadata_advertises_the_iss_parameter(self, oauth_client, path):
        """Both metadata locations promise ``iss`` on authorization responses, which the provider sends (RFC 9207)."""
        response = oauth_client.get(path)
        assert response.status_code == 200
        assert response.json()['authorization_response_iss_parameter_supported'] is True

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


class TestMountEmbedding:
    """``Gateway.mount`` wires OAuth and ``.well-known`` routes onto a host FastAPI app."""

    def test_mount_registers_well_known_routes(self, oauth_gateway):
        """``mount`` makes the host app serve the OAuth discovery metadata."""
        host = FastAPI()
        oauth_gateway.mount(host, transport='streamable-http')
        client = TestClient(host)

        response = client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        assert response.json()['authorization_endpoint'].endswith('/authorize')


class TestInstructions:
    """A server's ``instructions`` reach the client in the ``initialize`` result."""

    @pytest.mark.parametrize('instructions', ['Use get_pet_by_id before listing pets.', None])
    async def test_initialize_carries_the_configured_instructions(self, petstore_json_path, instructions):
        """Set instructions arrive verbatim, and unset ones leave the field out."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(petstore_json_path), instructions=instructions)

        async with Client(gateway.describe_servers()[0].mcp) as mcp_client:
            assert mcp_client.instructions == instructions


class TestTransports:
    """Every transport the CLI accepts starts and answers MCP, rather than starting far enough to log and then dying."""

    @pytest.mark.parametrize('transport', ['streamable-http', 'sse', 'stdio'])
    async def test_answers_initialize_and_lists_tools(self, gateway, petstore_json_path, transport):
        """A client of each transport completes ``initialize`` and lists the petstore tools."""
        with anyio.fail_after(10):
            async with _connect(gateway, petstore_json_path, transport) as mcp_client:
                server_info = mcp_client.server_info
                tools = await mcp_client.list_tools()

        assert server_info is not None
        assert server_info.name.startswith('petstore')
        assert tools.tools

    def test_sse_transport_emits_deprecation_warning(self, gateway):
        """Selecting the deprecated ``sse`` transport warns the caller."""
        with pytest.warns(DeprecationWarning, match='sse'):
            gateway.mount(FastAPI(), transport='sse')

    def test_streamable_http_transport_does_not_warn(self, gateway, recwarn):
        """The recommended ``streamable-http`` transport mounts without an SSE deprecation warning."""
        gateway.mount(FastAPI(), transport='streamable-http')
        sse_warnings = [
            warning
            for warning in recwarn
            if issubclass(warning.category, DeprecationWarning) and 'sse' in str(warning.message)
        ]
        assert not sse_warnings


class TestDNSRebindingProtection:
    """``Host`` / ``Origin`` checks on the MCP endpoints, which is how a DNS rebinding attack is refused."""

    @pytest.fixture
    def listed_gateway(self, petstore_json_path):
        """Petstore gateway that lists the one host and origin it is reached by."""
        config = GatewayConfig(
            servers=[ServerConfig(name='petstore', spec=str(petstore_json_path))],
            dns_rebinding_protection=DNSRebindingProtectionConfig(
                allowed_hosts=[GATEWAY_HOST], allowed_origins=[BROWSER_ORIGIN]
            ),
        )
        return Gateway.from_config(config)

    def test_loopback_bind_refuses_a_foreign_host(self, gateway):
        """A page re-pointing its own domain at 127.0.0.1 still sends that domain as ``Host``, so it is refused."""
        with TestClient(gateway._build_app(transport='streamable-http', host='127.0.0.1')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, 'localhost:8000') == 200

    def test_any_other_bind_accepts_any_host(self, gateway):
        """With nothing listed, a bind behind an unknown proxy keeps accepting whatever ``Host`` arrives."""
        with TestClient(gateway._build_app(transport='streamable-http', host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 200

    def test_listed_hosts_and_origins_are_enforced_on_any_bind(self, listed_gateway):
        """Listing hosts turns the check on even for a ``0.0.0.0`` bind, and accepts only what is listed."""
        with TestClient(listed_gateway._build_app(transport='streamable-http', host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, GATEWAY_HOST) == 200
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, GATEWAY_HOST, origin=BROWSER_ORIGIN) == 200
            assert _initialize_status(test_client, GATEWAY_HOST, origin=ATTACKER_URL) == 403

    def test_mounted_gateway_checks_only_when_hosts_are_listed(self):
        """A mounted gateway has no bind of its own to judge by, so only a listed host turns the check on."""
        unlisted = _transport_security(DNSRebindingProtectionConfig(), host=None)
        assert unlisted is not None
        assert not unlisted.enable_dns_rebinding_protection
        listed = _transport_security(DNSRebindingProtectionConfig(allowed_hosts=[GATEWAY_HOST]), host=None)
        assert listed is not None
        assert listed.enable_dns_rebinding_protection
        assert listed.allowed_hosts == [GATEWAY_HOST]

    def test_origins_without_hosts_are_refused(self):
        """Origins alone would turn on a check that refuses every ``Host``, so the config is refused instead."""
        with pytest.raises(ValueError, match='allowed_hosts'):
            DNSRebindingProtectionConfig(allowed_origins=[BROWSER_ORIGIN])


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

        tool = _tool(gateway, 'list_pets')
        result = await tool.run({'limit': 5}, context=_stub_context())

        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert captured['url'].startswith(f'{PETSTORE_URL}/pets')

        assert result.is_error is False
        assert json.loads(result.content[0].text) == [{'id': 1, 'name': 'fido'}]


class TestDynamicExposureEndToEnd:
    """Full assembly chain when ``exposure.style: dynamic`` swaps tools for the three meta-tools."""

    @pytest.fixture
    def dynamic_gateway(self, petstore_json_path):
        """Petstore gateway with the server flipped to dynamic exposure."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='petstore', spec=str(petstore_json_path), exposure=ExposureConfig(style='dynamic')),
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

        listing = (await tools['list_operations'].fn(ctx=_stub_context())).structured_content
        assert {entry['name'] for entry in listing['operations']} >= {'list_pets', 'get_pet_by_id'}

        described = (await tools['get_operation'].fn(name='list_pets', ctx=_stub_context())).structured_content
        assert described['input_schema']['properties']['limit']['type'] == 'integer'

        result = await tools['call_operation'].fn(name='list_pets', arguments={'limit': 5}, ctx=_stub_context())
        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert result.is_error is False
        assert json.loads(result.content[0].text) == [{'id': 1, 'name': 'fido'}]


class TestClientCredentialsFlowEndToEnd:
    """End-to-end behaviour of the ``client_credentials`` OAuth flow."""

    @pytest.fixture
    def cc_gateway(self, client_credentials_spec_path):
        """Single-server gateway whose spec declares only clientCredentials."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='petstore',
                    spec=str(client_credentials_spec_path),
                    auth=AuthConfig(
                        type='oauth2',
                        upstream=UpstreamAuthConfig(
                            client_id='gateway-id', client_secret='gateway-secret', scopes=['api']
                        ),
                    ),
                ),
            ],
        )
        return Gateway.from_config(config)

    @pytest.fixture
    def token_post(self, monkeypatch):
        """Replace the IdP's token endpoint with one that always issues ``cc-bearer-xyz``."""
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'access_token': 'cc-bearer-xyz', 'expires_in': 3600}
        token_response.text = ''
        token_post_mock = AsyncMock(return_value=token_response)
        monkeypatch.setattr(token_source_module.httpx.AsyncClient, 'post', token_post_mock, raising=False)
        return token_post_mock

    def test_setup_uses_client_credentials_flow(self, cc_gateway):
        """The gateway picks the client_credentials flow when only that flow is declared."""
        bundle = cc_gateway._servers[0]
        assert bundle.auth_provider is None
        assert bundle.auth_settings is None
        assert len(cc_gateway._shutdown_hooks) == 1

    async def test_tool_call_attaches_fetched_bearer(self, cc_gateway, token_post, mock_upstream):
        """A tool call fetches a token from the IdP, then forwards it as ``Authorization`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['authorization'] = request.headers.get('authorization')
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        await _tool(cc_gateway, 'list_pets').run({}, context=_stub_context())

        assert captured['authorization'] == 'Bearer cc-bearer-xyz'
        token_post.assert_awaited_once()
        post_args = token_post.await_args
        assert post_args is not None
        # The first positional arg is the URL, second positional or kwargs carry data.
        assert post_args.args[0] == TOKEN_URL or post_args.kwargs.get('url') == TOKEN_URL
        assert post_args.kwargs['data']['grant_type'] == 'client_credentials'

    async def test_token_is_cached_across_tool_calls(self, cc_gateway, token_post, mock_upstream):
        """Multiple tool calls share a single cached token (one POST to the IdP)."""
        mock_upstream(lambda request: httpx.Response(200, json=[]))

        tool = _tool(cc_gateway, 'list_pets')
        for _ in range(3):
            await tool.run({}, context=_stub_context())

        assert token_post.await_count == 1


class TestSpec20260728Adoption:
    """2026-07-28 spec adoptions layered on the v2 SDK: cache hints and stable ordering."""

    async def test_static_lists_carry_cache_hints(self, gateway):
        """``tools/list`` advertises the gateway's public, minutes-long freshness hint."""
        bundle = gateway._servers[0]
        async with Client(bundle.mcp) as mcp_client:
            result = await mcp_client.list_tools()
        dumped = result.model_dump(by_alias=True)
        assert dumped['ttlMs'] == 300_000
        assert dumped['cacheScope'] == 'public'

    def test_tool_registration_order_is_deterministic(self, petstore_json_path):
        """Two gateways built from the same spec register tools in the same order."""

        def tool_names() -> list[str]:
            config = GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(petstore_json_path))])
            mcp = Gateway.from_config(config)._servers[0].mcp
            return [tool.name for tool in mcp._tool_manager.list_tools()]

        first = tool_names()
        assert first, 'petstore should register at least one tool'
        assert first == tool_names()


class TestTraceContextPropagation:
    """W3C trace-context keys in a request's ``_meta`` are forwarded to the upstream HTTP call."""

    _TRACE: RequestParamsMeta = {
        'traceparent': '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
        'tracestate': 'rojo=00f067aa0ba902b7',
        'baggage': 'userId=alice',
    }

    async def test_trace_context_forwarded_to_upstream(self, gateway, mock_upstream):
        """traceparent / tracestate / baggage from the MCP call reach the upstream request verbatim."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = request.headers
            return httpx.Response(200, json=[])

        mock_upstream(handler)

        bundle = gateway._servers[0]
        async with Client(bundle.mcp, raise_exceptions=True) as client:
            await client.call_tool('list_pets', {}, meta=self._TRACE)

        headers = captured['headers']
        assert headers['traceparent'] == self._TRACE['traceparent']
        assert headers['tracestate'] == self._TRACE['tracestate']
        assert headers['baggage'] == self._TRACE['baggage']

    async def test_no_trace_context_forwards_no_trace_headers(self, gateway, mock_upstream):
        """A call without trace context in ``_meta`` adds no trace headers upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = request.headers
            return httpx.Response(200, json=[])

        mock_upstream(handler)

        bundle = gateway._servers[0]
        async with Client(bundle.mcp, raise_exceptions=True) as client:
            await client.call_tool('list_pets', {})

        headers = captured['headers']
        assert 'traceparent' not in headers
        assert 'tracestate' not in headers
        assert 'baggage' not in headers


class TestMovieShapingExample:
    """The examples/movie-shaping.yml config loads and shapes the TMDB surface as documented."""

    @pytest.fixture
    def movie_gateway(self, monkeypatch):
        """Build the gateway from the checked-in movie-shaping example."""
        monkeypatch.setenv('TMDB_TOKEN', 'test-token')
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        monkeypatch.chdir(repo_root)  # the config resolves its spec path relative to the working directory
        config = GatewayConfig.from_yaml(repo_root / 'examples' / 'movie-shaping.yml')
        return Gateway.from_config(config)

    async def test_discover_movies_surface_and_bridge(self, movie_gateway, mock_upstream):
        """The model sees only ``sort`` and ``page``, and the defaults, rename, and value-map reach the upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    'page': 1,
                    'results': [
                        {
                            'title': 'Dune',
                            'overview': 'Paul Atreides ...',
                            'release_date': '2021-10-22',
                            'vote_average': 8.0,
                            'poster_path': '/x.jpg',
                        }
                    ],
                },
            )

        mock_upstream(handler)
        bundle = movie_gateway._servers[0]
        async with Client(bundle.mcp) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'discover_movies')
            assert set(tool.input_schema['properties']) == {'sort', 'page'}
            result = await client.call_tool('discover_movies', {})

        assert captured['params']['sort_by'] == 'popularity.desc'  # default 'popular', renamed and value-mapped
        assert captured['params']['page'] == '1'  # x-mcp default sent when omitted
        assert captured['params']['include_adult'] == 'false'  # injected safety default
        assert captured['params']['language'] == 'en-US'  # injected locale
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert json.loads(content.text) == [
            {'title': 'Dune', 'overview': 'Paul Atreides ...', 'release_date': '2021-10-22', 'rating': 8.0}
        ]

    async def test_get_movie_details_injects_and_trims(self, movie_gateway, mock_upstream):
        """The model supplies only ``movie_id``, hidden query defaults are injected, and the body is trimmed."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            captured['params'] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    'id': 550,
                    'title': 'Fight Club',
                    'overview': 'A ticking-time-bomb insomniac ...',
                    'release_date': '1999-10-15',
                    'vote_average': 8.4,
                    'budget': 63000000,
                    'credits': {'cast': [{'name': 'Edward Norton'}, {'name': 'Brad Pitt'}]},
                },
            )

        mock_upstream(handler)
        bundle = movie_gateway._servers[0]
        async with Client(bundle.mcp) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'get_movie_details')
            assert set(tool.input_schema['properties']) == {'movie_id'}
            result = await client.call_tool('get_movie_details', {'movie_id': 550})

        assert '/movie/550' in captured['url']
        assert captured['params']['append_to_response'] == 'credits'
        assert captured['params']['language'] == 'en-US'
        assert result.structured_content == {
            'title': 'Fight Club',
            'overview': 'A ticking-time-bomb insomniac ...',
            'release_date': '1999-10-15',
            'rating': 8.4,
            'cast': ['Edward Norton', 'Brad Pitt'],
        }


class TestTokenExchangeDiscovery:
    """Discovery documents for a server whose authorization is delegated to an external issuer."""

    @pytest.fixture
    def delegating_client(self, petstore_json_path):
        """Gateway whose petstore server validates tokens from an issuer it does not own."""
        return _delegating_client(petstore_json_path)

    def test_protected_resource_names_the_external_issuer(self, delegating_client):
        """The document points clients at the issuer, and names this endpoint as the resource."""
        response = delegating_client.get('/.well-known/oauth-protected-resource/petstore')

        assert response.status_code == 200
        data = response.json()
        assert data['resource'] == f'{GATEWAY_URL}/petstore/mcp'
        assert data['authorization_servers'] == [ISSUER]

    def test_gateway_does_not_claim_to_be_an_authorization_server(self, delegating_client):
        """The AS metadata path 404s, since the gateway serves no /authorize or /token here.

        Publishing a document would send clients to endpoints this app does not have.
        """
        response = delegating_client.get('/.well-known/oauth-authorization-server/petstore')

        assert response.status_code == 404
        assert 'not an authorization server' in response.json()['error']

    def test_no_oauth_endpoints_are_mounted(self, delegating_client):
        """``/authorize`` and ``/token`` under the mount path belong to the issuer, not the gateway."""
        assert delegating_client.get('/petstore/authorize').status_code == 404
        assert delegating_client.post('/petstore/token').status_code == 404

    def test_healthz_reports_the_endpoint_as_protected(self, delegating_client):
        """A delegating server is still OAuth-protected, so health must not report it as open."""
        servers = delegating_client.get('/healthz').json()['servers']

        assert servers[0]['auth'] == 'oauth2'


class TestRejectedTokenResponse:
    """A token the gateway refuses produces a 401 challenge, not a server error.

    The MCP SDK's bearer backend does not guard the ``verify_token`` call,
    so an exception raised there reaches uvicorn as a 500.
    A client reads that as the server being broken and keeps resending the same credential,
    where a 401 would have sent it to re-authorize.
    """

    RESOURCE = f'{GATEWAY_URL}/petstore/mcp'

    @pytest.fixture
    def delegating_app(self, petstore_json_path, signing_key):
        """Gateway delegating to an external issuer, with that issuer's key resolvable."""
        jwk = MagicMock()
        jwk.key = signing_key.public_key()
        jwk.key_type = 'RSA'
        jwk_client = MagicMock()
        jwk_client.get_signing_key_from_jwt.return_value = jwk
        return _delegating_client(petstore_json_path, jwk_client)

    def _token(self, signing_key, **claims) -> str:
        """A token signed by the issuer's key, where a claim set to ``None`` is left out."""
        payload = {'iss': ISSUER, 'aud': self.RESOURCE, 'sub': 'user-1', 'exp': int(time.time()) + 300, **claims}
        return jwt.encode({k: v for k, v in payload.items() if v is not None}, signing_key, algorithm='RS256')

    def _post(self, client, token: str):
        return client.post(
            '/petstore/mcp',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json',
            },
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        )

    def test_expired_token_is_a_challenge_not_a_server_error(self, delegating_app, signing_key):
        """Expiry is the ordinary path here, since the issuer owns the lifetimes.

        Keycloak defaults to five minutes, so every session reaches this within minutes of starting.
        A 500 leaves the client resending the same dead token forever.
        """
        expired = self._token(signing_key, exp=int(time.time()) - 60)

        response = self._post(delegating_app, expired)

        assert response.status_code == 401
        assert 'invalid_token' in response.headers.get('WWW-Authenticate', '')

    @pytest.mark.parametrize(
        ('claims', 'reason'),
        [
            ({'aud': API_URL}, 'audience naming the upstream instead of this endpoint'),
            ({'iss': 'https://other.example.com'}, 'another issuer'),
            ({'aud': None}, 'no audience at all'),
        ],
    )
    def test_every_rejection_reason_is_a_challenge(self, delegating_app, signing_key, claims, reason):
        """No rejection path may reach the client as a server error."""
        token = self._token(signing_key, **claims)

        response = self._post(delegating_app, token)

        assert response.status_code == 401, f'rejected for {reason}'
        assert 'invalid_token' in response.headers.get('WWW-Authenticate', '')

    def test_a_credential_that_is_not_a_jwt_is_also_a_challenge(self, delegating_app):
        """An opaque credential is unrecognised rather than invalid, and still must not 500."""
        response = self._post(delegating_app, 'an-opaque-session-token')

        assert response.status_code == 401
