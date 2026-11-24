import contextlib
import dataclasses
import json
import logging
import pathlib
import typing
from unittest.mock import AsyncMock, patch

import httpx
import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import InvalidScopeError, OAuthToken
from mcp.types import CallToolResult
from starlette.testclient import TestClient

from openapi_mcp_gateway.auth.mcp.authorization_code import McpAuthorizationCodeServer
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import GATEWAY_URL
from tests.mcp_client import Browser, MemoryTokenStorage, authorization_code_auth, connect
from tests.oauth import authorization_code_gateway, query_value, server_of, sign_in, upstream_token_response
from tests.serving import serve, serve_gateway
from tests.tool_result import text_of


# The protocol revisions a client may speak.
# Only 2026-07-28 carries the routing headers an HTTP-layer scope challenge is read from.
MODERN = '2026-07-28'
LEGACY = 'legacy'

# The pre-registered clients every scoped server declares, by what each may do to pets.
# ``stepper`` starts with ``pets:write`` and may step up for the rest.
READER = 'reader'
DELETER = 'deleter'
WRITER = 'writer'
STEPPER = 'stepper'
CLIENTS = {
    READER: {'scopes': ['pets:read']},
    DELETER: {'scopes': ['pets:read', 'pets:delete']},
    WRITER: {'scopes': ['pets:write']},
    STEPPER: {'scopes': ['pets:read', 'pets:write', 'pets:delete'], 'default_scopes': ['pets:write']},
}

# The tools a token holding only ``pets:read`` sees on the petstore fixture.
READ_TOOLS = ['list_pets', 'get_pet_by_id']

# The challenges a token is answered with when its scopes lack the one named.
DELETE_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="pets:delete", '
    f'resource_metadata="{GATEWAY_URL}/.well-known/oauth-protected-resource/petstore/mcp", '
    'error_description="This operation needs one of these scopes: pets:delete."'
)
READ_CHALLENGE = DELETE_CHALLENGE.replace('pets:delete', 'pets:read')

# A pet read through the resource the petstore's ``getPetById`` is promoted to.
PET_RESOURCE = 'petstore://pets/7'

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
        url: str = GATEWAY_URL,
        other_clients: dict[str, typing.Any] | None = None,
    ) -> Gateway:
        return authorization_code_gateway(
            spec,
            clients or CLIENTS,
            operation_scopes=operation_scopes if operation_scopes is not None else {'auto': 'tags'},
            upstream=upstream,
            exposure={'style': style, 'promote_resources': promote_resources},
            url=url,
            other_clients=other_clients,
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

    async def test_a_client_lists_what_it_may_step_up_for(self, gateway):
        """A tool hidden until the token holds its scope would never be called, so the step-up would never start."""
        async with _connect(gateway, STEPPER, MODERN) as session:
            assert 'delete_pet' in await _tool_names(session)

    async def test_the_list_is_cached_per_token(self, gateway):
        async with _connect(gateway, READER, MODERN) as session:
            listed = await session.client.list_tools()
        assert listed.model_dump(by_alias=True)['cacheScope'] == 'private'


class TestScopeRejection:
    """A call outside the token's scopes is refused, with a challenge wherever the client can read one."""

    async def test_a_modern_client_is_challenged_over_http(self, gateway):
        """The ``403`` names the missing scope, so the client can step up for it."""
        async with _connect(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_pet', {'petId': '1'})
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_client_that_may_never_hold_the_scope_is_challenged_for_none(self, gateway):
        """Naming a scope the client cannot be granted would only send it through a consent page in vain."""
        async with _connect(gateway, READER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_pet', {'petId': '1'})
        challenge = session.refusal.headers['www-authenticate']
        assert 'scope=' not in challenge.replace('insufficient_scope', '')
        assert 'This client may not be granted any scope this operation needs.' in challenge

    async def test_an_operation_no_scope_grants_keeps_its_own_refusal(self, gateway):
        """The client is not to blame when no scope at all opens the operation."""
        async with _connect(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('listAdminPets', {})
        assert 'No scope grants this operation.' in session.refusal.headers['www-authenticate']

    async def test_a_legacy_client_receives_a_tool_error(self, gateway, upstream_requests):
        """A client older than 2026-07-28 sends no routing headers, so the refusal arrives inside the result."""
        async with _connect(gateway, READER, LEGACY) as session:
            result = await session.client.call_tool('delete_pet', {'petId': '1'})
        assert result.is_error
        assert text_of(result) == 'This operation needs one of these scopes: pets:delete.'
        assert upstream_requests == []

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_call_within_the_scopes_reaches_the_upstream(self, gateway, upstream_requests, mode):
        async with _connect(gateway, DELETER, mode) as session:
            result = await session.client.call_tool('delete_pet', {'petId': '1'})
        assert not result.is_error
        assert [(request.method, request.url.path) for request in upstream_requests] == [('DELETE', '/v1/pets/1')]


class TestDynamicExposure:
    """The meta-tools show what the client may hold scopes for, and call only what the token's scopes open."""

    @pytest.fixture
    def gateway(self, make_gateway) -> Gateway:
        return make_gateway(style='dynamic')

    async def test_search_and_get_show_only_open_operations(self, gateway):
        async with _connect(gateway, READER, MODERN) as session:
            searched = await session.client.call_tool('search_operations', {})
            hidden = await session.client.call_tool('get_operation', {'name': 'delete_pet'})
        listing = json.loads(text_of(searched))
        assert [operation['name'] for operation in listing['operations']] == READ_TOOLS
        assert listing['tags'] == [{'name': 'pets', 'operations': 2}]
        assert hidden.is_error

    async def test_search_and_get_show_what_the_client_may_step_up_for(self, gateway):
        async with _connect(gateway, STEPPER, MODERN) as session:
            searched = await session.client.call_tool('search_operations', {})
            described = await session.client.call_tool('get_operation', {'name': 'delete_pet'})
        assert 'delete_pet' in [operation['name'] for operation in json.loads(text_of(searched))['operations']]
        assert not described.is_error

    async def test_a_modern_client_is_challenged_for_the_operation_it_calls(self, gateway):
        """``Mcp-Param-Operation`` names the operation, so the challenge needs no look at the body."""
        async with _connect(gateway, STEPPER, MODERN) as session:
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
        assert 'pets:delete' in text_of(result)
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
        async with _connect(gateway, STEPPER, MODERN) as session:
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
        ('client_id', 'requested', 'granted'),
        [
            (DELETER, None, 'pets:read pets:delete'),
            (DELETER, 'pets:delete pets:write', 'pets:delete'),
            (STEPPER, None, 'pets:write'),
            (STEPPER, 'pets:write pets:delete', 'pets:write pets:delete'),
        ],
    )
    async def test_the_token_response_states_the_granted_scope(self, gateway, client_id, requested, granted):
        """Asking for nothing grants the client's defaults, and a request is narrowed to what the client may hold."""
        token = await _sign_in(gateway, client_id, requested)
        assert token.scope == granted

    @pytest.mark.parametrize(('client_id', 'granted'), [(DELETER, 'pets:read pets:delete'), (STEPPER, 'pets:write')])
    async def test_the_sdk_client_is_granted_the_clients_defaults(self, make_gateway, client_id, granted):
        """The SDK's own client names no scope on its first sign-in, so it starts on the client's defaults."""
        token = await _sign_in_through_the_sdk(make_gateway, client_id)
        assert token.scope == granted

    async def test_a_request_for_nothing_the_client_may_hold_is_refused(self, gateway):
        with pytest.raises(InvalidScopeError):
            await _sign_in(gateway, DELETER, 'pets:write')

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

    def test_other_clients_granted_an_undefined_scope_are_refused_at_startup(self, make_gateway):
        with pytest.raises(ValueError, match=r'other_clients: pets:raed'):
            make_gateway(other_clients={'scopes': ['pets:raed']})


@pytest.fixture
def make_enforcing_gateway(make_gateway, secured_spec) -> typing.Callable[..., Gateway]:
    """A gateway enforcing the upstream grant, asking for at most both pet scopes unless ``upstream`` says otherwise."""

    def make(style: str = 'static', url: str = GATEWAY_URL, **upstream: typing.Any) -> Gateway:
        settings = {'enforce_scopes': True, 'scopes': ['read:pets', 'write:pets'], **upstream}
        return make_gateway(spec=secured_spec, style=style, upstream=settings, url=url)

    return make


class TestUpstreamGrant:
    """With ``enforce_scopes``, an operation also needs the upstream scopes its ``security`` declares."""

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
        """The client already holds the MCP scopes, so stepping up cannot change what the upstream granted."""
        async with _connect(make_enforcing_gateway(style), DELETER, MODERN, 'read:pets') as session:
            result = await session.client.call_tool(tool, arguments)
        assert result.is_error
        # The meta-tool's refusal arrives prefixed by the SDK, as any error a tool raises does.
        assert DELETE_UPSTREAM_REFUSAL in text_of(result)
        assert upstream_requests == []

    async def test_missing_mcp_scopes_are_challenged_first(self, make_enforcing_gateway):
        """A client steps up for the MCP scopes, so it hears about them before the upstream grant."""
        async with _connect(make_enforcing_gateway(), STEPPER, MODERN, 'read:pets') as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_pet', {'petId': '1'})
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_tool_the_client_may_step_up_for_stays_listed_before_the_upstream_grants_it(
        self, make_enforcing_gateway
    ):
        """The sign-in a step-up starts asks the upstream for what ``delete_pet`` needs, so hiding it would stall."""
        async with _connect(make_enforcing_gateway(), STEPPER, MODERN, 'write:pets') as session:
            assert 'delete_pet' in await _tool_names(session)

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_tool_no_sign_in_can_open_is_not_exposed(self, make_enforcing_gateway, caplog, mode):
        """No sign-in within ``read:pets`` opens ``create_pet`` or ``delete_pet``, so a step-up would stall."""
        with caplog.at_level(logging.WARNING):
            gateway = make_enforcing_gateway(scopes=['read:pets'])
        assert 'no sign-in can open 2 operation(s)' in caplog.text
        async with _connect(gateway, STEPPER, mode, 'read:pets') as session:
            names = await _tool_names(session)
        assert 'list_pets' in names
        assert not {'create_pet', 'delete_pet'} & set(names)

    def test_startup_warns_about_operations_the_requested_scopes_cannot_open(self, make_gateway, secured_spec, caplog):
        """Without ``enforce_scopes`` the gateway does not check the grant, so the call may still succeed."""
        with caplog.at_level(logging.WARNING):
            make_gateway(spec=secured_spec, upstream={'scopes': ['read:pets']})
        assert '"deleter" can reach 1 operation(s) the requested upstream scopes cannot open' in caplog.text
        assert '"reader"' not in caplog.text

    @pytest.mark.parametrize(('key', 'value'), [('enforce_scopes', True), ('required_scopes', ['offline_access'])])
    def test_a_flow_without_a_user_grant_refuses_the_sign_in_settings(self, client_credentials_spec_path, key, value):
        auth = {'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', key: value}}
        server = {'name': 'svc', 'spec': str(client_credentials_spec_path), 'auth': auth}
        with pytest.raises(ValueError, match=rf'auth\.upstream\.{key} needs auth\.upstream\.flow authorization_code'):
            Gateway.from_config(GatewayConfig.model_validate({'url': GATEWAY_URL, 'servers': [server]}))


class TestUpstreamDownscoping:
    """Each sign-in asks the upstream for what the operations its MCP scopes open declare, within the ceiling."""

    @pytest.mark.parametrize(
        ('client_id', 'requested'),
        [(READER, 'read:pets'), (WRITER, 'write:pets'), (DELETER, 'read:pets write:pets')],
    )
    @pytest.mark.parametrize('ceiling', [['read:pets', 'write:pets'], []], ids=['listed', 'declared'])
    async def test_a_sign_in_asks_for_what_its_mcp_scopes_need(
        self, make_enforcing_gateway, client_id, requested, ceiling
    ):
        """Without ``auth.upstream.scopes``, the ceiling is every scope the operations declare."""
        browser = await _sign_in_with_the_sdk(lambda url: make_enforcing_gateway(url=url, scopes=ceiling), client_id)
        assert [query_value(url, 'scope') for url in browser.upstream_sign_ins] == [requested]

    async def test_a_required_scope_is_asked_for_at_every_sign_in(self, make_enforcing_gateway):
        browser = await _sign_in_with_the_sdk(
            lambda url: make_enforcing_gateway(url=url, required_scopes=['offline_access']), READER
        )
        assert query_value(browser.upstream_sign_ins[0], 'scope') == 'offline_access read:pets'

    async def test_without_enforce_scopes_every_sign_in_asks_for_the_whole_list(self, make_gateway, secured_spec):
        upstream = {'scopes': ['read:pets', 'write:pets']}
        browser = await _sign_in_with_the_sdk(
            lambda url: make_gateway(spec=secured_spec, upstream=upstream, url=url), READER
        )
        assert query_value(browser.upstream_sign_ins[0], 'scope') == 'read:pets write:pets'

    async def test_a_step_up_asks_the_upstream_for_what_the_wider_grant_needs(
        self, make_enforcing_gateway, upstream_requests
    ):
        """The consent page marks the upstream scope the step-up adds, and the call then reaches the upstream."""
        browser, result = await _step_up_with_the_sdk(
            lambda url: make_enforcing_gateway(url=url), STEPPER, 'delete_pet', {'petId': '1'}
        )
        assert not result.is_error
        assert [query_value(url, 'scope') for url in browser.upstream_sign_ins] == [
            'write:pets',
            'read:pets write:pets',
        ]
        assert 'read:pets</code><span class="new">New</span>' in browser.consent_pages[-1]
        assert [request.method for request in upstream_requests] == ['DELETE']


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


def _provider(gateway: Gateway) -> McpAuthorizationCodeServer:
    """The authorization server of the gateway's one server."""
    return server_of(gateway.describe_servers()[0].auth_provider)


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


async def _sign_in_with_the_sdk(build: typing.Callable[[str], Gateway], client_id: str) -> Browser:
    """Sign ``client_id`` in with the SDK's own client to the gateway ``build`` makes, and return the browser used."""
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(build) as base_url:
            endpoint = f'{base_url}/petstore/mcp'
            browser = Browser(f'{base_url}/petstore')
            async with connect(endpoint, authorization_code_auth(endpoint, browser, client_id)):
                pass
    return browser


async def _step_up_with_the_sdk(
    build: typing.Callable[[str], Gateway], client_id: str, tool: str, arguments: dict[str, typing.Any]
) -> tuple[Browser, CallToolResult]:
    """Sign ``client_id`` in with the SDK's own client and call ``tool``, which its default scopes cannot use.

    The upstream grants whatever each sign-in asks for, so only the MCP scopes stand in the way of the first call.
    """
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(build) as base_url:
            endpoint = f'{base_url}/petstore/mcp'
            browser = Browser(f'{base_url}/petstore')
            async with connect(endpoint, authorization_code_auth(endpoint, browser, client_id)) as client:
                result = await client.call_tool(tool, arguments)
    return browser, result


async def _sign_in_through_the_sdk(make_gateway: typing.Callable[..., Gateway], client_id: str) -> OAuthToken:
    """Sign ``client_id`` in with the SDK's own client, through consent and the upstream, and return its tokens."""
    storage = MemoryTokenStorage()
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(lambda url: make_gateway(url=url)) as base_url:
            endpoint = f'{base_url}/petstore/mcp'
            auth = authorization_code_auth(endpoint, Browser(f'{base_url}/petstore'), client_id, storage=storage)
            async with connect(endpoint, auth):
                pass
    assert storage.tokens is not None
    return storage.tokens
