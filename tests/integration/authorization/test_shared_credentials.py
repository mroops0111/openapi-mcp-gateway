import json
import logging
import pathlib
import typing
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from mcp import Client

from openapi_mcp_gateway.auth.upstream import client_credentials as client_credentials_module
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from tests.constants import (
    API_URL,
    TOKEN_URL,
)
from tests.gateway import server_mcp, tool_names
from tests.spec import TRELLO_SCHEMES, write_board_spec
from tests.tool_result import text_of


# The two query keys ``TRELLO_SCHEMES`` declares, as a config gives them.
TRELLO_CREDENTIALS = {'APIKey': {'value': 'the-key'}, 'APIToken': {'value': 'the-token'}}


class TestCredentialPlacement:
    """Each credential reaches the upstream where its scheme says, whatever the scheme's location."""

    async def test_two_query_keys_ride_on_every_call(self, tmp_path, upstream_requests):
        spec = write_board_spec(tmp_path, schemes=TRELLO_SCHEMES, security=[{'APIKey': [], 'APIToken': []}])

        await _call(_gateway(spec, credentials=TRELLO_CREDENTIALS), 'get_board', {'board_id': 'b1'})

        assert dict(upstream_requests[0].url.params) == {'key': 'the-key', 'token': 'the-token'}

    async def test_a_header_key_and_a_cookie_ride_together(self, tmp_path, upstream_requests):
        schemes = {
            'key': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'},
            'session': {'type': 'apiKey', 'in': 'cookie', 'name': 'sid'},
        }
        spec = write_board_spec(tmp_path, schemes=schemes, security=[{'key': [], 'session': []}])
        gateway = _gateway(
            spec,
            credentials={'key': {'value': 'k'}, 'session': {'value': 's'}},
            headers={'Cookie': 'theme=dark'},
        )

        await _call(gateway, 'get_board', {'board_id': 'b1'})

        request = upstream_requests[0]
        assert request.headers['x-api-key'] == 'k'
        assert request.headers['cookie'] == 'theme=dark; sid=s'

    async def test_an_argument_for_the_same_header_never_replaces_the_credential(self, tmp_path, upstream_requests):
        """An older spec also lists its key header as a parameter, which the model may fill in."""
        spec = tmp_path / 'boards.json'
        spec.write_text(
            json.dumps(
                {
                    'openapi': '3.1.0',
                    'info': {'title': 'Boards', 'version': '1'},
                    'servers': [{'url': API_URL}],
                    'components': {'securitySchemes': {'key': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'}}},
                    'security': [{'key': []}],
                    'paths': {
                        '/boards': {
                            'get': {
                                'operationId': 'listBoards',
                                'parameters': [{'name': 'x-api-key', 'in': 'header', 'schema': {'type': 'string'}}],
                                'responses': {'200': {'description': 'ok'}},
                            }
                        }
                    },
                }
            )
        )

        await _call(_gateway(spec, credentials={'key': {'value': 'k'}}), 'list_boards', {'x_api_key': 'placeholder'})

        assert upstream_requests[0].headers.get_list('x-api-key') == ['k']

    async def test_a_swagger_definition_places_its_key(self, tmp_path, upstream_requests):
        spec = write_board_spec(tmp_path, swagger_definitions=TRELLO_SCHEMES, security=[{'APIKey': [], 'APIToken': []}])

        await _call(_gateway(spec, credentials=TRELLO_CREDENTIALS), 'get_board', {'board_id': 'b1'})

        assert dict(upstream_requests[0].url.params) == {'key': 'the-key', 'token': 'the-token'}


class TestSecurityOverrides:
    """A spec that declares no scheme, or a wrong one, is overridden in its own shape."""

    async def test_a_declared_scheme_and_root_security_serve_a_spec_without_either(self, tmp_path, upstream_requests):
        spec = write_board_spec(tmp_path)
        gateway = _gateway(
            spec,
            credentials={'internal': {'value': 'k'}},
            security_schemes={'internal': {'type': 'apiKey', 'in': 'header', 'name': 'X-Internal-Key'}},
            security=[{'internal': []}],
        )

        await _call(gateway, 'get_board', {'board_id': 'b1'})

        assert upstream_requests[0].headers['x-internal-key'] == 'k'

    async def test_an_overridden_scheme_replaces_the_one_the_spec_declares(self, tmp_path, upstream_requests):
        spec = write_board_spec(tmp_path, schemes={'APIKey': {'type': 'apiKey', 'in': 'header', 'name': 'key'}})
        gateway = _gateway(
            spec,
            credentials={'APIKey': {'value': 'k'}},
            security_schemes={'APIKey': TRELLO_SCHEMES['APIKey']},
            security=[{'APIKey': []}],
        )

        await _call(gateway, 'get_board', {'board_id': 'b1'})

        assert dict(upstream_requests[0].url.params) == {'key': 'k'}
        assert 'key' not in upstream_requests[0].headers

    async def test_add_server_takes_the_same_overrides(self, tmp_path, upstream_requests):
        gateway = Gateway()
        gateway.add_server(
            name='boards',
            spec=str(write_board_spec(tmp_path)),
            security_schemes={'internal': {'type': 'apiKey', 'in': 'header', 'name': 'X-Internal-Key'}},
            security=[{'internal': []}],
            auth={'upstream': {'credentials': {'internal': {'value': 'k'}}}},
        )

        await _call(gateway, 'get_board', {'board_id': 'b1'})

        assert upstream_requests[0].headers['x-internal-key'] == 'k'


class TestStartupCheck:
    """A config that cannot serve an exposed operation, or configures what none uses, never starts."""

    async def test_leaving_the_operation_out_lets_the_server_start(self, tmp_path):
        spec = write_board_spec(
            tmp_path,
            schemes=TRELLO_SCHEMES,
            operations={'getBoard': [{'APIKey': [], 'APIToken': []}], 'updateBoard': []},
        )

        gateway = _gateway(spec, policy={'deny': ['getBoard']})

        assert await tool_names(gateway) == ['update_board']


class TestRedaction:
    """A query credential never reaches the client, even in the error a refused call returns."""

    async def test_an_upstream_error_hides_the_query_credentials(self, tmp_path, mock_upstream):
        mock_upstream(lambda request: httpx.Response(401, json={'message': 'invalid key'}))
        spec = write_board_spec(tmp_path, schemes=TRELLO_SCHEMES, security=[{'APIKey': [], 'APIToken': []}])

        result = await _call(_gateway(spec, credentials=TRELLO_CREDENTIALS), 'get_board', {'board_id': 'b1'})

        assert result.is_error
        assert 'the-key' not in text_of(result)
        assert 'the-token' not in text_of(result)
        assert f'{API_URL}/boards/b1?key=[REDACTED]&token=[REDACTED]' in text_of(result)

    async def test_the_request_log_hides_the_query_credentials(self, tmp_path, upstream_requests, caplog):
        spec = write_board_spec(tmp_path, schemes=TRELLO_SCHEMES, security=[{'APIKey': [], 'APIToken': []}])

        with caplog.at_level(logging.INFO, logger='httpx'):
            await _call(_gateway(spec, credentials=TRELLO_CREDENTIALS), 'get_board', {'board_id': 'b1'})

        logged = caplog.text
        assert 'key=[REDACTED]&token=[REDACTED]' in logged
        assert 'the-key' not in logged
        assert 'the-token' not in logged


class TestBesideAFlow:
    """Static credentials ride beside a flow's token, each operation carrying what its security settled on."""

    async def test_an_api_key_operation_carries_its_key_and_an_oauth_one_does_not(
        self, tmp_path, monkeypatch, upstream_requests
    ):
        token_response = MagicMock(status_code=200, text='')
        token_response.json.return_value = {'access_token': 'flow-token', 'expires_in': 3600}
        monkeypatch.setattr(
            client_credentials_module.httpx.AsyncClient, 'post', AsyncMock(return_value=token_response), raising=False
        )
        schemes = {
            'api_key': {'type': 'apiKey', 'in': 'header', 'name': 'api_key'},
            'oauth': {
                'type': 'oauth2',
                'flows': {'clientCredentials': {'tokenUrl': f'{API_URL}/token', 'scopes': {'write': 'w'}}},
            },
        }
        spec = write_board_spec(
            tmp_path,
            schemes=schemes,
            operations={
                'getBoard': [{'api_key': []}, {'oauth': ['write']}],
                'updateBoard': [{'oauth': ['write']}],
            },
        )
        upstream = {
            'type': 'oauth2',
            'flow': 'client_credentials',
            'client_id': 'cid',
            'client_secret': 'sec',
            'credentials': {'api_key': {'value': 'k'}},
        }
        gateway = _gateway(spec, upstream=upstream)

        await _call(gateway, 'get_board', {'board_id': 'b1'})
        await _call(gateway, 'update_board', {'board_id': 'b1'})

        get_board, update_board = upstream_requests
        assert get_board.headers['api_key'] == 'k'
        assert 'authorization' not in get_board.headers
        assert 'api_key' not in update_board.headers
        assert update_board.headers['authorization'] == 'Bearer flow-token'


class TestClientCredentialsFlowEndToEnd:
    """End-to-end behaviour of the ``client_credentials`` OAuth flow."""

    @pytest.fixture
    def cc_gateway(self, client_credentials_spec_path):
        """Single-server gateway whose spec declares only clientCredentials."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='shop',
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
        monkeypatch.setattr(client_credentials_module.httpx.AsyncClient, 'post', token_post_mock, raising=False)
        return token_post_mock

    def test_the_token_source_is_closed_at_shutdown(self, cc_gateway):
        """The flow hands the gateway a shutdown hook, which closes the token source's own HTTP client."""
        assert cc_gateway.describe_servers()[0].auth.on_shutdown is not None

    async def test_tool_call_attaches_fetched_bearer(self, cc_gateway, token_post, mock_upstream):
        """A tool call fetches a token from the IdP, then forwards it as ``Authorization`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['authorization'] = request.headers.get('authorization')
            return httpx.Response(200, json=[{'id': 1, 'name': 'tote'}])

        mock_upstream(handler)

        async with Client(server_mcp(cc_gateway), raise_exceptions=True) as client:
            result = await client.call_tool('list_products', {})

        assert not result.is_error
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

        async with Client(server_mcp(cc_gateway), raise_exceptions=True) as client:
            results = [await client.call_tool('list_products', {}) for _ in range(3)]

        assert not any(result.is_error for result in results)
        assert token_post.await_count == 1


def _gateway(
    spec: pathlib.Path,
    *,
    credentials: dict[str, typing.Any] | None = None,
    upstream: dict[str, typing.Any] | None = None,
    **server: typing.Any,
) -> Gateway:
    upstream = upstream or ({'credentials': credentials} if credentials else {})
    config = {'servers': [{'name': 'boards', 'spec': str(spec), 'auth': {'upstream': upstream}, **server}]}
    return Gateway.from_config(GatewayConfig.model_validate(config))


async def _call(gateway: Gateway, tool: str, arguments: dict[str, typing.Any]):
    async with Client(server_mcp(gateway)) as client:
        return await client.call_tool(tool, arguments)
