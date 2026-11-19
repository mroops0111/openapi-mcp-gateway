import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp import Client
from mcp.types import RequestParamsMeta, TextContent

from openapi_mcp_gateway.auth.upstream import token_source as token_source_module
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    ExposureConfig,
    GatewayConfig,
    SearchConfig,
    SearchFieldWeights,
    ServerConfig,
    UpstreamAuthConfig,
)
from openapi_mcp_gateway.upstream_http import UpstreamHttpClient
from tests.constants import (
    PETSTORE_URL,
    TOKEN_URL,
)
from tests.gateway import auth_shutdown_hooks
from tests.tool_result import text_of


class TestEndToEndToolInvocation:
    """Exercise the full assembly chain from spec to operations, tool registration, and the upstream HTTP call."""

    async def test_list_pets_calls_upstream_with_query_params(self, gateway, mock_upstream):
        """Invoking the generated ``listPets`` tool reaches the upstream URL with the right query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        async with Client(gateway._servers[0].mcp) as client:
            result = await client.call_tool('list_pets', {'limit': 5})

        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert captured['url'].startswith(f'{PETSTORE_URL}/pets')

        assert result.is_error is False
        assert json.loads(text_of(result)) == [{'id': 1, 'name': 'fido'}]


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

    async def test_only_three_meta_tools_registered(self, dynamic_gateway):
        """An MCP client sees just ``search_operations``, ``get_operation``, ``call_operation``."""
        async with Client(dynamic_gateway._servers[0].mcp) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {'search_operations', 'get_operation', 'call_operation'}

    async def test_search_field_weights_reach_the_ranking(self, petstore_json_path):
        """A weight of 0 on every field but ``path`` ranks by path alone, so a word only in summaries finds nothing."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='petstore',
                    spec=str(petstore_json_path),
                    exposure=ExposureConfig(
                        style='dynamic',
                        search=SearchConfig(
                            field_weights=SearchFieldWeights(name=0, summary=0, description=0, tags=0, path=1)
                        ),
                    ),
                ),
            ],
        )
        async with Client(Gateway.from_config(config)._servers[0].mcp) as client:
            by_path = (await client.call_tool('search_operations', {'query': 'pets'})).structured_content
            by_summary_word = (await client.call_tool('search_operations', {'query': 'list'})).structured_content
        assert by_path is not None and by_path['operations']
        assert by_summary_word is not None and by_summary_word['operations'] == []

    async def test_list_then_get_then_call_roundtrip(self, dynamic_gateway, mock_upstream):
        """An LLM-style search, get, and call sequence drives an upstream call with correct query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        async with Client(dynamic_gateway._servers[0].mcp) as client:
            listing = (await client.call_tool('search_operations', {'query': 'list pets'})).structured_content
            assert listing is not None and listing['operations'][0]['name'] == 'list_pets'

            described = (await client.call_tool('get_operation', {'name': 'list_pets'})).structured_content
            assert described is not None and described['input_schema']['properties']['limit']['type'] == 'integer'

            result = await client.call_tool('call_operation', {'name': 'list_pets', 'arguments': {'limit': 5}})
        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert result.is_error is False
        assert json.loads(text_of(result)) == [{'id': 1, 'name': 'fido'}]


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
                        upstream=UpstreamAuthConfig(
                            type='oauth2', client_id='gateway-id', client_secret='gateway-secret', scopes=['api']
                        )
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
        assert len(auth_shutdown_hooks(cc_gateway)) == 1

    async def test_tool_call_attaches_fetched_bearer(self, cc_gateway, token_post, mock_upstream):
        """A tool call fetches a token from the IdP, then forwards it as ``Authorization`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['authorization'] = request.headers.get('authorization')
            return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])

        mock_upstream(handler)

        async with Client(cc_gateway._servers[0].mcp) as client:
            await client.call_tool('list_pets', {})

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

        async with Client(cc_gateway._servers[0].mcp) as client:
            for _ in range(3):
                await client.call_tool('list_pets', {})

        assert token_post.await_count == 1


class TestUpstreamHttpClientLifecycle:
    """Each server keeps one upstream client across calls and closes it when the gateway shuts down."""

    async def test_calls_reuse_one_client_closed_at_shutdown(self, gateway, upstream_requests, monkeypatch):
        """Two calls go through one client, and running the shutdown hooks closes it."""
        constructed: list[UpstreamHttpClient] = []
        original_init = UpstreamHttpClient.__init__

        def recording_init(self, *args, **kwargs):
            constructed.append(self)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(UpstreamHttpClient, '__init__', recording_init)

        async with Client(gateway._servers[0].mcp, raise_exceptions=True) as client:
            await client.call_tool('list_pets', {})
            await client.call_tool('list_pets', {})
        await gateway._run_shutdown_hooks()

        assert len(upstream_requests) == 2
        assert len(constructed) == 1
        assert constructed[0]._client.is_closed


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
