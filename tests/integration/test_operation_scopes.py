import contextlib
import dataclasses
import json
import pathlib
import typing

import httpx
import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import GATEWAY_URL
from tests.oauth import sign_in
from tests.serving import serve


# The protocol revisions a client may speak.
# Only 2026-07-28 carries the routing headers an HTTP-layer scope challenge is read from.
MODERN = '2026-07-28'
LEGACY = 'legacy'

# The pre-registered clients every scoped server declares, by what each may do to pets.
READER = 'reader'
DELETER = 'deleter'
CLIENTS = {READER: {'scopes': ['pets:read']}, DELETER: {'scopes': ['pets:read', 'pets:delete']}}

# The tools a token holding only ``pets:read`` sees on the petstore fixture.
READ_TOOLS = ['list_pets', 'get_pet_by_id']

# The challenge a reader is answered with when it calls ``delete_pet``.
DELETE_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="pets:delete", '
    f'resource_metadata="{GATEWAY_URL}/.well-known/oauth-protected-resource/petstore", '
    'error_description="This operation needs one of these scopes: pets:delete."'
)


@dataclasses.dataclass
class _Session:
    """An MCP client connected with one token, and every HTTP response it received, in order."""

    client: Client
    responses: list[httpx2.Response]

    @property
    def refusal(self) -> httpx2.Response:
        """The last response, which refused the call that just failed."""
        return self.responses[-1]


@pytest.fixture
def make_gateway(petstore_json_path: pathlib.Path) -> typing.Callable[..., Gateway]:
    """Build a petstore gateway whose ``authorization_code`` server defines ``scopes`` for ``clients``."""

    def make(
        scopes: dict[str, typing.Any] | None = None,
        clients: dict[str, typing.Any] | None = None,
        style: str = 'static',
    ) -> Gateway:
        auth = {
            'type': 'oauth2',
            'upstream': {'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
            'scopes': scopes if scopes is not None else {'auto': 'tags'},
            'clients': clients or CLIENTS,
        }
        server = {'name': 'petstore', 'spec': str(petstore_json_path), 'auth': auth, 'exposure': {'style': style}}
        return Gateway.from_config(GatewayConfig.model_validate({'url': GATEWAY_URL, 'servers': [server]}))

    return make


@pytest.fixture
def gateway(make_gateway) -> Gateway:
    """The petstore gateway with tag-derived scopes and static exposure."""
    return make_gateway()


async def _sign_in(gateway: Gateway, client_id: str, requested: str | None = None):
    """Sign ``client_id`` in through the gateway's own authorization server, asking for ``requested``."""
    provider = gateway.describe_servers()[0].auth_provider
    assert provider is not None
    client = await provider.get_client(client_id)
    assert client is not None
    return await sign_in(provider, client, scopes=client.validate_scope(requested))


@contextlib.asynccontextmanager
async def _connect(gateway: Gateway, client_id: str, mode: str) -> typing.AsyncIterator[_Session]:
    """Serve ``gateway`` over HTTP and connect an MCP client speaking ``mode``, signed in as ``client_id``."""
    token = await _sign_in(gateway, client_id)
    responses: list[httpx2.Response] = []

    async def record(response: httpx2.Response) -> None:
        responses.append(response)

    http_client = httpx2.AsyncClient(
        headers={'Authorization': f'Bearer {token.access_token}'}, event_hooks={'response': [record]}
    )
    async with serve(gateway._build_app(transport='streamable-http')) as base_url:
        transport = streamable_http_client(f'{base_url}/petstore/mcp', http_client=http_client)
        async with Client(transport, mode=mode) as client:
            yield _Session(client, responses)


async def _tool_names(session: _Session) -> list[str]:
    """The tools ``tools/list`` shows the session's token, in registration order."""
    return [tool.name for tool in (await session.client.list_tools()).tools]


def _text(result: CallToolResult) -> str:
    """The text a tool result carries."""
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


class TestToolFiltering:
    """``tools/list`` shows each token only the tools its scopes open."""

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    @pytest.mark.parametrize(('client_id', 'expected'), [(READER, READ_TOOLS), (DELETER, [*READ_TOOLS, 'delete_pet'])])
    async def test_each_client_lists_only_what_its_scopes_open(self, gateway, mode, client_id, expected):
        """The untagged operations stay hidden, since ``unscoped`` defaults to ``deny``."""
        async with _connect(gateway, client_id, mode) as session:
            assert await _tool_names(session) == expected

    async def test_unscoped_allow_opens_untagged_operations_to_every_token(self, make_gateway):
        gateway = make_gateway(scopes={'auto': 'tags', 'unscoped': 'allow'})
        async with _connect(gateway, READER, MODERN) as session:
            assert await _tool_names(session) == [*READ_TOOLS, 'create_pet_with_owner', 'listAdminPets']

    async def test_a_manual_scope_adds_a_way_in(self, make_gateway):
        """A client holding only the hand-written scope reaches what the derived scope also opens."""
        gateway = make_gateway(
            scopes={'auto': 'tags', 'manual': {'cleanup': {'description': 'Remove pets', 'operations': ['DELETE *']}}},
            clients={'janitor': {'scopes': ['cleanup']}},
        )
        async with _connect(gateway, 'janitor', MODERN) as session:
            assert await _tool_names(session) == ['delete_pet']

    async def test_the_list_is_cached_per_token(self, gateway):
        async with _connect(gateway, READER, MODERN) as session:
            listed = await session.client.list_tools()
        assert listed.model_dump(by_alias=True)['cacheScope'] == 'private'


class TestScopeRejection:
    """A call outside the token's scopes is refused, with a challenge wherever the client can read one."""

    async def test_a_modern_client_is_challenged_over_http(self, gateway):
        """The ``403`` names the missing scope, so the client can step up for it."""
        async with _connect(gateway, READER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_pet', {'petId': '1'})
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_legacy_client_receives_a_tool_error(self, gateway, upstream_requests):
        """A client older than 2026-07-28 sends no routing headers, so the refusal arrives inside the result."""
        async with _connect(gateway, READER, LEGACY) as session:
            result = await session.client.call_tool('delete_pet', {'petId': '1'})
        assert result.is_error
        assert _text(result) == 'This operation needs one of these scopes: pets:delete.'
        assert upstream_requests == []

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_call_within_the_scopes_reaches_the_upstream(self, gateway, upstream_requests, mode):
        async with _connect(gateway, DELETER, mode) as session:
            result = await session.client.call_tool('delete_pet', {'petId': '1'})
        assert not result.is_error
        assert [(request.method, request.url.path) for request in upstream_requests] == [('DELETE', '/v1/pets/1')]


class TestDynamicExposure:
    """The meta-tools show and call only the operations the token's scopes open."""

    @pytest.fixture
    def gateway(self, make_gateway) -> Gateway:
        return make_gateway(style='dynamic')

    async def test_search_and_get_show_only_open_operations(self, gateway):
        async with _connect(gateway, READER, MODERN) as session:
            searched = await session.client.call_tool('search_operations', {})
            hidden = await session.client.call_tool('get_operation', {'name': 'delete_pet'})
        listing = json.loads(_text(searched))
        assert [operation['name'] for operation in listing['operations']] == READ_TOOLS
        assert listing['tags'] == [{'name': 'pets', 'operations': 2}]
        assert hidden.is_error

    async def test_a_modern_client_is_challenged_for_the_operation_it_calls(self, gateway):
        """``Mcp-Param-Operation`` names the operation, so the challenge needs no look at the body."""
        async with _connect(gateway, READER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('call_operation', {'name': 'delete_pet', 'arguments': {'petId': '1'}})
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_legacy_client_receives_a_tool_error(self, gateway, upstream_requests):
        async with _connect(gateway, READER, LEGACY) as session:
            result = await session.client.call_tool(
                'call_operation', {'name': 'delete_pet', 'arguments': {'petId': '1'}}
            )
        assert result.is_error
        assert 'pets:delete' in _text(result)
        assert upstream_requests == []


class TestAuthorizationServer:
    """With pre-registered clients the gateway grants each its own scopes and advertises none up front."""

    @pytest.mark.parametrize(
        ('requested', 'granted'),
        [(None, 'pets:read pets:delete'), ('pets:delete', 'pets:delete'), ('pets:write', 'pets:read pets:delete')],
    )
    async def test_the_token_response_states_the_granted_scope(self, gateway, requested, granted):
        token = await _sign_in(gateway, DELETER, requested)
        assert token.scope == granted

    def test_discovery_advertises_no_scopes_and_no_registration(self, gateway):
        """Discovery happens before the client is known, so no single scope set would be right."""
        http = TestClient(gateway._build_app(transport='streamable-http'))
        resource = http.get('/.well-known/oauth-protected-resource/petstore').json()
        server = http.get('/.well-known/oauth-authorization-server/petstore').json()
        assert 'scopes_supported' not in resource
        assert 'scopes_supported' not in server
        assert 'registration_endpoint' not in server
        assert http.post('/petstore/register', json={'redirect_uris': ['http://localhost/cb']}).status_code in {
            httpx.codes.NOT_FOUND,
            httpx.codes.METHOD_NOT_ALLOWED,
        }

    def test_a_client_granted_an_undefined_scope_is_refused_at_startup(self, make_gateway):
        with pytest.raises(ValueError, match=r'reader: pets:raed'):
            make_gateway(clients={READER: {'scopes': ['pets:raed']}})
