import contextlib
import dataclasses
import json
import logging
import pathlib
import typing

import httpx
import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from openapi_mcp_gateway.auth.flows import AuthorizationCodeProvider
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import GATEWAY_URL
from tests.oauth import authorization_code_gateway, sign_in
from tests.serving import serve


# The protocol revisions a client may speak.
# Only 2026-07-28 carries the routing headers an HTTP-layer scope challenge is read from.
MODERN = '2026-07-28'
LEGACY = 'legacy'

# The pre-registered clients every scoped server declares, by what each may do to pets.
READER = 'reader'
DELETER = 'deleter'
WRITER = 'writer'
CLIENTS = {
    READER: {'scopes': ['pets:read']},
    DELETER: {'scopes': ['pets:read', 'pets:delete']},
    WRITER: {'scopes': ['pets:write']},
}

# The tools a token holding only ``pets:read`` sees on the petstore fixture.
READ_TOOLS = ['list_pets', 'get_pet_by_id']

# The challenges a token is answered with when its scopes lack the one named.
DELETE_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="pets:delete", '
    f'resource_metadata="{GATEWAY_URL}/.well-known/oauth-protected-resource/petstore", '
    'error_description="This operation needs one of these scopes: pets:delete."'
)
READ_CHALLENGE = DELETE_CHALLENGE.replace('pets:delete', 'pets:read')

# A pet read through the resource the petstore's ``getPetById`` is promoted to.
PET_RESOURCE = 'petstore://pets/7'

# The upstream scopes each petstore operation declares in its OpenAPI ``security``, keyed by operationId.
UPSTREAM_SECURITY = {
    'listPets': [{'oauth2': ['read:pets']}],
    'getPetById': [{'oauth2': ['read:pets']}],
    'createPet': [{'oauth2': ['write:pets']}],
    'deletePet': [{'oauth2': ['read:pets', 'write:pets']}],
}

# Why a token whose upstream grant lacks what ``deletePet`` declares is refused.
DELETE_UPSTREAM_REFUSAL = (
    'The upstream token lacks the scopes deletePet needs (read:pets write:pets). '
    'Add them to auth.upstream.scopes and sign in again, or correct its security under operations.deletePet.security.'
)


@pytest.fixture
def make_gateway(petstore_json_path: pathlib.Path) -> typing.Callable[..., Gateway]:
    """Build a petstore gateway whose ``authorization_code`` server defines ``scopes`` for ``clients``.

    ``upstream`` is merged into ``auth.upstream``, and ``spec`` replaces the petstore fixture.
    """

    def make(
        operation_scopes: dict[str, typing.Any] | None = None,
        clients: dict[str, typing.Any] | None = None,
        style: str = 'static',
        promote_resources: bool = False,
        upstream: dict[str, typing.Any] | None = None,
        spec: pathlib.Path = petstore_json_path,
    ) -> Gateway:
        return authorization_code_gateway(
            spec,
            clients or CLIENTS,
            operation_scopes=operation_scopes if operation_scopes is not None else {'auto': 'tags'},
            upstream=upstream,
            exposure={'style': style, 'promote_resources': promote_resources},
        )

    return make


@pytest.fixture
def gateway(make_gateway) -> Gateway:
    """The petstore gateway with tag-derived scopes and static exposure."""
    return make_gateway()


class TestToolFiltering:
    """``tools/list`` shows each token only the tools its scopes open."""

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    @pytest.mark.parametrize(('client_id', 'expected'), [(READER, READ_TOOLS), (DELETER, [*READ_TOOLS, 'delete_pet'])])
    async def test_each_client_lists_only_what_its_scopes_open(self, gateway, mode, client_id, expected):
        """The untagged operations stay hidden, since ``unscoped`` defaults to ``deny``."""
        async with _connect(gateway, client_id, mode) as session:
            assert await _tool_names(session) == expected

    async def test_unscoped_allow_opens_untagged_operations_to_every_token(self, make_gateway):
        gateway = make_gateway(operation_scopes={'auto': 'tags', 'unscoped': 'allow'})
        async with _connect(gateway, READER, MODERN) as session:
            assert await _tool_names(session) == [*READ_TOOLS, 'create_pet_with_owner', 'listAdminPets']

    async def test_a_manual_scope_adds_a_way_in(self, make_gateway):
        """A client holding only the hand-written scope reaches what the derived scope also opens."""
        gateway = make_gateway(
            operation_scopes={
                'auto': 'tags',
                'manual': {'cleanup': {'description': 'Remove pets', 'operations': ['DELETE *']}},
            },
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


class TestPromotedResources:
    """A resource promoted from an operation is listed and read only under the scopes that operation requires."""

    @pytest.fixture
    def gateway(self, make_gateway) -> Gateway:
        return make_gateway(promote_resources=True)

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    @pytest.mark.parametrize(
        ('client_id', 'resources', 'templates'),
        [(READER, ['petstore://pets'], ['petstore://pets/{petId}']), (WRITER, [], [])],
    )
    async def test_each_client_lists_only_what_its_scopes_open(self, gateway, mode, client_id, resources, templates):
        async with _connect(gateway, client_id, mode) as session:
            listed = await session.client.list_resources()
            listed_templates = await session.client.list_resource_templates()
        assert [str(resource.uri) for resource in listed.resources] == resources
        assert [template.uri_template for template in listed_templates.resource_templates] == templates
        assert listed.model_dump(by_alias=True)['cacheScope'] == 'private'

    async def test_a_modern_client_is_challenged_over_http(self, gateway, upstream_requests):
        """``Mcp-Name`` carries the URI, which the template it expands from decides."""
        async with _connect(gateway, WRITER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.read_resource(PET_RESOURCE)
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == READ_CHALLENGE
        assert upstream_requests == []

    async def test_a_legacy_client_receives_a_protocol_error(self, gateway, upstream_requests):
        async with _connect(gateway, WRITER, LEGACY) as session:
            with pytest.raises(MCPError, match='pets:read'):
                await session.client.read_resource(PET_RESOURCE)
        assert upstream_requests == []

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_read_within_the_scopes_reaches_the_upstream(self, gateway, upstream_requests, mode):
        async with _connect(gateway, READER, mode) as session:
            await session.client.read_resource(PET_RESOURCE)
        assert [(request.method, request.url.path) for request in upstream_requests] == [('GET', '/v1/pets/7')]


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


class TestUpstreamGrant:
    """With ``enforce_scopes``, an operation also needs the upstream scopes its ``security`` declares."""

    @pytest.fixture
    def secured_spec(self, petstore_json_path: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
        """The petstore fixture with each operation's upstream scopes declared."""
        spec = json.loads(petstore_json_path.read_text())
        for path_item in spec['paths'].values():
            for operation in path_item.values():
                if isinstance(operation, dict) and operation.get('operationId') in UPSTREAM_SECURITY:
                    operation['security'] = UPSTREAM_SECURITY[operation['operationId']]
        path = tmp_path / 'secured-petstore.json'
        path.write_text(json.dumps(spec))
        return path

    @pytest.fixture
    def make_enforcing_gateway(self, make_gateway, secured_spec) -> typing.Callable[..., Gateway]:
        """A gateway enforcing the upstream grant, requesting both pet scopes unless ``upstream`` says otherwise."""

        def make(style: str = 'static', **upstream: typing.Any) -> Gateway:
            settings = {'enforce_scopes': True, 'scopes': ['read:pets', 'write:pets'], **upstream}
            return make_gateway(spec=secured_spec, style=style, upstream=settings)

        return make

    @pytest.mark.parametrize(
        ('upstream_scope', 'expected'),
        [('read:pets', READ_TOOLS), ('read:pets write:pets', [*READ_TOOLS, 'delete_pet'])],
    )
    async def test_the_list_shows_only_what_the_grant_opens(self, make_enforcing_gateway, upstream_scope, expected):
        async with _connect(make_enforcing_gateway(), DELETER, MODERN, upstream_scope) as session:
            assert await _tool_names(session) == expected

    @pytest.mark.parametrize(
        ('style', 'tool', 'arguments'),
        [
            ('static', 'delete_pet', {'petId': '1'}),
            ('dynamic', 'call_operation', {'name': 'delete_pet', 'arguments': {'petId': '1'}}),
        ],
    )
    async def test_a_short_grant_is_a_tool_error_rather_than_a_challenge(
        self, make_enforcing_gateway, upstream_requests, style, tool, arguments
    ):
        """Stepping up at the gateway cannot change what the upstream granted, so no client is challenged."""
        async with _connect(make_enforcing_gateway(style), DELETER, MODERN, 'read:pets') as session:
            result = await session.client.call_tool(tool, arguments)
        assert result.is_error
        # The meta-tool's refusal arrives prefixed by the SDK, as any error a tool raises does.
        assert DELETE_UPSTREAM_REFUSAL in _text(result)
        assert upstream_requests == []

    async def test_missing_mcp_scopes_are_challenged_first(self, make_enforcing_gateway):
        """A client can step up for the MCP scopes and not for the upstream grant, so it hears about them first."""
        async with _connect(make_enforcing_gateway(), READER, MODERN, 'read:pets') as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_pet', {'petId': '1'})
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    def test_without_requested_scopes_the_gateway_requests_what_the_operations_declare(self, make_enforcing_gateway):
        gateway = make_enforcing_gateway(scopes=[])
        assert _provider(gateway).upstream.scopes == ['read:pets', 'write:pets']

    def test_startup_warns_about_operations_the_requested_scopes_cannot_open(self, make_gateway, secured_spec, caplog):
        """The check runs without ``enforce_scopes`` as well, since the upstream refuses the call anyway."""
        with caplog.at_level(logging.WARNING):
            make_gateway(spec=secured_spec, upstream={'scopes': ['read:pets']})
        assert '"deleter" can reach 1 operation(s) the requested upstream scopes cannot open' in caplog.text
        assert '"reader"' not in caplog.text

    def test_enforcing_a_flow_without_a_user_grant_is_refused(self, client_credentials_spec_path):
        auth = {
            'type': 'oauth2',
            'upstream': {'client_id': 'gateway', 'client_secret': 'secret', 'enforce_scopes': True},
        }
        server = {'name': 'svc', 'spec': str(client_credentials_spec_path), 'auth': auth}
        with pytest.raises(ValueError, match=r'enforce_scopes needs auth\.flow authorization_code'):
            Gateway.from_config(GatewayConfig.model_validate({'url': GATEWAY_URL, 'servers': [server]}))


@dataclasses.dataclass
class _Session:
    """An MCP client connected with one token, and every HTTP response it received, in order."""

    client: Client
    responses: list[httpx2.Response]

    @property
    def refusal(self) -> httpx2.Response:
        """The last response, which refused the call that just failed."""
        return self.responses[-1]


async def _sign_in(gateway: Gateway, client_id: str, requested: str | None = None, upstream_scope: str | None = None):
    """Sign ``client_id`` in through the gateway's own authorization server, asking for ``requested``.

    The upstream states ``upstream_scope`` as the scope it granted, or omits it when ``None``.
    """
    provider = _provider(gateway)
    client = await provider.get_client(client_id)
    assert client is not None
    return await sign_in(provider, client, scopes=client.validate_scope(requested), upstream_scope=upstream_scope)


def _provider(gateway: Gateway) -> AuthorizationCodeProvider:
    """The authorization server of the gateway's one server."""
    provider = gateway.describe_servers()[0].auth_provider
    assert provider is not None
    return provider


@contextlib.asynccontextmanager
async def _connect(
    gateway: Gateway, client_id: str, mode: str, upstream_scope: str | None = None
) -> typing.AsyncIterator[_Session]:
    """Serve ``gateway`` over HTTP and connect an MCP client speaking ``mode``, signed in as ``client_id``.

    The upstream granted ``upstream_scope``, or what the gateway requested when ``None``.
    """
    token = await _sign_in(gateway, client_id, upstream_scope=upstream_scope)
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
