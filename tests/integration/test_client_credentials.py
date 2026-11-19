import base64
import contextlib
import pathlib
import typing

import httpx
import httpx2
import pytest
from mcp import Client, MCPError
from starlette.testclient import TestClient

from openapi_mcp_gateway.auth.mcp.client_credentials import CLIENT_CREDENTIALS_EXTENSION
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import GATEWAY_URL
from tests.mcp_client import MemoryTokenStorage, client_credentials_auth, connect
from tests.serving import bind_loopback_socket, build_socket_url, serve


# The agents the gateway issues tokens to, each with its own secret, by what each may do to pets.
READER = 'reader'
WRITER = 'writer'
SECRETS = {READER: 'reader-secret', WRITER: 'writer-secret'}
CLIENTS = {
    READER: {'client_secret': SECRETS[READER], 'scopes': ['pets:read']},
    WRITER: {'client_secret': SECRETS[WRITER], 'scopes': ['pets:read', 'pets:write']},
}

# The one credential every agent's calls carry upstream.
UPSTREAM_TOKEN = 'upstream-pat'

# The tools a token holding only ``pets:read`` sees on the petstore fixture.
READ_TOOLS = ['list_pets', 'get_pet_by_id']


@pytest.fixture
def make_gateway(petstore_json_path: pathlib.Path) -> typing.Callable[..., Gateway]:
    """Build a petstore gateway calling its upstream with one bearer token, issuing MCP tokens to ``CLIENTS``."""

    def make(url: str = GATEWAY_URL) -> Gateway:
        server = {
            'name': 'petstore',
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {'type': 'bearer', 'token': UPSTREAM_TOKEN},
                'mcp': {'clients': CLIENTS, 'operation_scopes': {'auto': 'tags'}},
            },
        }
        return Gateway.from_config(GatewayConfig.model_validate({'url': url, 'servers': [server]}))

    return make


@pytest.fixture
def http(make_gateway) -> TestClient:
    """The gateway's HTTP app, served in process."""
    return TestClient(make_gateway()._build_app(transport='streamable-http'))


class TestDiscovery:
    """A client finds the token endpoint and how to authenticate at it without being told."""

    def test_the_authorization_server_offers_only_client_credentials(self, http):
        metadata = http.get('/.well-known/oauth-authorization-server/petstore').json()

        assert metadata['issuer'] == f'{GATEWAY_URL}/petstore'
        assert metadata['token_endpoint'] == f'{GATEWAY_URL}/petstore/token'
        assert metadata['grant_types_supported'] == ['client_credentials']
        assert metadata['token_endpoint_auth_methods_supported'] == ['client_secret_basic', 'client_secret_post']
        assert 'registration_endpoint' not in metadata
        # No authorization response is ever sent, so there is no ``iss`` in one to announce.
        assert 'authorization_response_iss_parameter_supported' not in metadata

    def test_the_protected_resource_names_the_gateway(self, http):
        document = http.get('/.well-known/oauth-protected-resource/petstore').json()

        assert document['resource'] == f'{GATEWAY_URL}/petstore/mcp'
        assert document['authorization_servers'] == [f'{GATEWAY_URL}/petstore']

    def test_no_user_can_sign_in(self, http):
        response = http.get('/petstore/authorize')

        assert response.status_code == 400
        assert response.json()['error'] == 'unauthorized_client'


class TestTokenEndpoint:
    """The token endpoint answers as RFC 6749 §4.4 and §5 describe."""

    @pytest.mark.parametrize('method', ['basic', 'post'])
    def test_a_client_obtains_every_scope_it_may_hold(self, http, method):
        """Nothing would approve a narrower start, so a request for no scope is granted them all."""
        response = _request_token(http, WRITER, method=method)

        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        token = response.json()
        assert token['scope'] == 'pets:read pets:write'
        assert 'refresh_token' not in token

    def test_a_requested_scope_is_narrowed_to_the_clients_own(self, http):
        assert _request_token(http, WRITER, scope='pets:write pets:delete').json()['scope'] == 'pets:write'

    def test_a_scope_the_client_may_never_hold_is_refused(self, http):
        response = _request_token(http, READER, scope='pets:delete')

        assert response.status_code == 400
        assert response.json()['error'] == 'invalid_scope'

    def test_a_wrong_secret_is_refused(self, http):
        response = _request_token(http, READER, secret='guessed')

        assert response.status_code == 401
        assert response.json()['error'] == 'invalid_client'
        assert response.headers['www-authenticate'] == 'Basic'

    def test_another_grant_is_refused(self, http):
        response = _request_token(http, READER, grant_type='authorization_code')

        assert response.json()['error'] == 'unsupported_grant_type'

    def test_a_token_for_another_resource_is_refused(self, http):
        response = _request_token(http, READER, resource='https://elsewhere.example.com/mcp')

        assert response.json()['error'] == 'invalid_target'

    def test_the_canonical_resource_is_accepted(self, http):
        assert _request_token(http, READER, resource=f'{GATEWAY_URL}/petstore/mcp').status_code == 200

    def test_a_foreign_resource_beside_the_canonical_one_is_refused(self, http):
        """RFC 8707 lets ``resource`` repeat, so every one named is checked."""
        form = {
            'grant_type': 'client_credentials',
            'resource': ['https://elsewhere.example.com/mcp', f'{GATEWAY_URL}/petstore/mcp'],
        }
        response = http.post('/petstore/token', data=form, auth=(READER, SECRETS[READER]))

        assert response.json()['error'] == 'invalid_target'


class TestMcpEndpoint:
    """Each agent reaches only the operations its scopes open, through the one upstream credential."""

    async def test_a_call_without_a_token_is_refused(self, make_gateway):
        async with (
            serve(make_gateway()._build_app(transport='streamable-http')) as base_url,
            httpx.AsyncClient() as http,
        ):
            response = await http.post(f'{base_url}/petstore/mcp', json={})
        assert response.status_code == 401
        assert 'resource_metadata=' in response.headers['www-authenticate']

    async def test_a_token_issued_for_another_resource_is_refused(self, make_gateway):
        """The SDK compares the resource each token records against this endpoint."""
        gateway = make_gateway()
        async with (
            serve(gateway._build_app(transport='streamable-http')) as base_url,
            httpx.AsyncClient() as http,
        ):
            token = await _obtain_token(base_url, READER)
            record = await gateway._store.get('mcp_access_token', token)
            await gateway._store.set('mcp_access_token', token, {**record, 'resource': f'{base_url}/other/mcp'})
            response = await http.post(
                f'{base_url}/petstore/mcp', json={}, headers={'Authorization': f'Bearer {token}'}
            )
        assert response.status_code == 401

    async def test_each_agent_lists_only_what_its_scopes_open(self, make_gateway, upstream_requests):
        async with _serve(make_gateway) as base_url, _connect(base_url, READER) as client:
            assert [tool.name for tool in (await client.list_tools()).tools] == READ_TOOLS
            await client.call_tool('list_pets', {})
            with pytest.raises(MCPError):
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert [request.headers['authorization'] for request in upstream_requests] == [f'Bearer {UPSTREAM_TOKEN}']

    async def test_the_server_advertises_the_extension(self, make_gateway):
        """A client probing ``server/discover`` learns it may obtain a token with its own credentials."""
        async with _serve(make_gateway) as base_url, _connect(base_url, READER, mode='auto') as client:
            assert CLIENT_CREDENTIALS_EXTENSION in (client.server_capabilities.extensions or {})

    async def test_an_agent_steps_up_without_a_user(self, make_gateway, upstream_requests):
        """Within its own scopes the agent steps up by asking again, since no user has anything to approve."""
        storage = MemoryTokenStorage()
        async with _serve(make_gateway) as base_url, _connect(base_url, WRITER, storage=storage) as client:
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert not result.is_error
        assert storage.tokens is not None
        assert set((storage.tokens.scope or '').split()) == {'pets:read', 'pets:write'}
        assert [request.method for request in upstream_requests] == ['POST']


def _request_token(
    http: TestClient,
    client_id: str,
    *,
    method: str = 'basic',
    secret: str | None = None,
    scope: str | None = None,
    grant_type: str = 'client_credentials',
    resource: str | None = None,
) -> httpx2.Response:
    """POST a token request for ``client_id``, authenticating by HTTP Basic or in the form."""
    secret = secret if secret is not None else SECRETS[client_id]
    form = {'grant_type': grant_type, 'client_id': client_id}
    headers = {}
    if method == 'basic':
        headers['Authorization'] = 'Basic ' + base64.b64encode(f'{client_id}:{secret}'.encode()).decode()
    else:
        form['client_secret'] = secret
    if scope is not None:
        form['scope'] = scope
    if resource is not None:
        form['resource'] = resource
    return http.post('/petstore/token', data=form, headers=headers)


async def _obtain_token(base_url: str, client_id: str) -> str:
    """An access token for ``client_id`` from the token endpoint of the gateway served at ``base_url``."""
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f'{base_url}/petstore/token',
            data={'grant_type': 'client_credentials'},
            auth=(client_id, SECRETS[client_id]),
        )
    response.raise_for_status()
    return response.json()['access_token']


@contextlib.asynccontextmanager
async def _serve(make_gateway: typing.Callable[..., Gateway]) -> typing.AsyncIterator[str]:
    """Serve a gateway built by ``make_gateway`` at the address it publishes, and yield that address."""
    sock = bind_loopback_socket()
    gateway = make_gateway(url=build_socket_url(sock))
    async with serve(gateway._build_app(transport='streamable-http'), sock) as base_url:
        yield base_url


def _connect(
    base_url: str, client_id: str, mode: typing.Any = '2026-07-28', storage: MemoryTokenStorage | None = None
) -> contextlib.AbstractAsyncContextManager[Client]:
    """An MCP SDK client of the petstore server at ``base_url``, signed in as ``client_id`` and speaking ``mode``.

    It asks for ``pets:read`` and steps up from there.
    """
    endpoint = f'{base_url}/petstore/mcp'
    auth = client_credentials_auth(endpoint, client_id, SECRETS[client_id], 'pets:read', storage)
    return connect(endpoint, auth, mode=mode)
