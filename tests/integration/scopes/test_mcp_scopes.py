import json

import httpx
import pytest
from mcp import MCPError
from mcp.shared.auth import InvalidScopeError
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from tests.integration.scopes.clients import (
    DELETE_CHALLENGE,
    DELETER,
    LEGACY,
    MODERN,
    PRODUCT_RESOURCE,
    READ_CHALLENGE,
    READ_TOOLS,
    READER,
    STEPPER,
    WRITER,
    connect_as,
    listed_tool_names,
    sign_in_as,
    sign_in_through_the_sdk,
)
from tests.tool_result import text_of


class TestToolFiltering:
    """``tools/list`` shows each token only the tools its scopes open."""

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    @pytest.mark.parametrize(
        ('client_id', 'expected'), [(READER, READ_TOOLS), (DELETER, [*READ_TOOLS, 'delete_product'])]
    )
    async def test_each_client_lists_only_what_its_scopes_open(self, gateway, mode, client_id, expected):
        """The untagged operations stay hidden, since ``unscoped`` defaults to ``deny``."""
        async with connect_as(gateway, client_id, mode) as session:
            assert await listed_tool_names(session) == expected

    async def test_unscoped_allow_opens_untagged_operations_to_every_token(self, make_gateway):
        gateway = make_gateway(operation_scopes={'auto': 'tags', 'unscoped': 'allow'})
        async with connect_as(gateway, READER, MODERN) as session:
            assert await listed_tool_names(session) == [*READ_TOOLS, 'create_product_with_owner', 'listAdminProducts']

    async def test_a_manual_scope_adds_a_way_in(self, make_gateway):
        """A client holding only the hand-written scope reaches what the derived scope also opens."""
        gateway = make_gateway(
            operation_scopes={
                'auto': 'tags',
                'manual': {'cleanup': {'description': 'Remove products', 'operations': ['DELETE *']}},
            },
            clients={'janitor': {'scopes': ['cleanup']}},
        )
        async with connect_as(gateway, 'janitor', MODERN) as session:
            assert await listed_tool_names(session) == ['delete_product']

    async def test_a_client_lists_what_it_may_step_up_for(self, gateway):
        """A tool hidden until the token holds its scope would never be called, so the step-up would never start."""
        async with connect_as(gateway, STEPPER, MODERN) as session:
            assert 'delete_product' in await listed_tool_names(session)

    async def test_the_list_is_cached_per_token(self, gateway):
        async with connect_as(gateway, READER, MODERN) as session:
            listed = await session.client.list_tools()
        assert listed.model_dump(by_alias=True)['cacheScope'] == 'private'


class TestScopeRejection:
    """A call outside the token's scopes is refused, with a challenge wherever the client can read one."""

    async def test_a_modern_client_is_challenged_over_http(self, gateway):
        """The ``403`` names the missing scope, so the client can step up for it."""
        async with connect_as(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_product', {'productId': '1'})
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_client_that_may_never_hold_the_scope_is_challenged_for_none(self, gateway):
        """Naming a scope the client cannot be granted would only send it through a consent page in vain."""
        async with connect_as(gateway, READER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_product', {'productId': '1'})
        challenge = session.refusal.headers['www-authenticate']
        assert 'scope=' not in challenge.replace('insufficient_scope', '')
        assert 'This client may not be granted any scope this operation needs.' in challenge

    async def test_an_operation_no_scope_grants_keeps_its_own_refusal(self, gateway):
        """The client is not to blame when no scope at all opens the operation."""
        async with connect_as(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('listAdminProducts', {})
        assert 'No scope grants this operation.' in session.refusal.headers['www-authenticate']

    async def test_a_legacy_client_receives_a_tool_error(self, gateway, upstream_requests):
        """A client older than 2026-07-28 sends no routing headers, so the refusal arrives inside the result."""
        async with connect_as(gateway, READER, LEGACY) as session:
            result = await session.client.call_tool('delete_product', {'productId': '1'})
        assert result.is_error
        assert text_of(result) == 'This operation needs one of these scopes: products:delete.'
        assert upstream_requests == []

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_call_within_the_scopes_reaches_the_upstream(self, gateway, upstream_requests, mode):
        async with connect_as(gateway, DELETER, mode) as session:
            result = await session.client.call_tool('delete_product', {'productId': '1'})
        assert not result.is_error
        assert [(request.method, request.url.path) for request in upstream_requests] == [('DELETE', '/v1/products/1')]


class TestDynamicExposure:
    """The meta-tools show what the client may hold scopes for, and call only what the token's scopes open."""

    @pytest.fixture
    def gateway(self, make_gateway) -> Gateway:
        return make_gateway(style='dynamic')

    async def test_search_and_get_show_only_open_operations(self, gateway):
        async with connect_as(gateway, READER, MODERN) as session:
            searched = await session.client.call_tool('search_operations', {})
            hidden = await session.client.call_tool('get_operation', {'name': 'delete_product'})
        listing = json.loads(text_of(searched))
        assert [operation['name'] for operation in listing['operations']] == READ_TOOLS
        assert listing['tags'] == [{'name': 'products', 'operations': 2}]
        assert hidden.is_error

    async def test_search_and_get_show_what_the_client_may_step_up_for(self, gateway):
        async with connect_as(gateway, STEPPER, MODERN) as session:
            searched = await session.client.call_tool('search_operations', {})
            described = await session.client.call_tool('get_operation', {'name': 'delete_product'})
        assert 'delete_product' in [operation['name'] for operation in json.loads(text_of(searched))['operations']]
        assert not described.is_error

    async def test_a_modern_client_is_challenged_for_the_operation_it_calls(self, gateway):
        """``Mcp-Param-Operation`` names the operation, so the challenge needs no look at the body."""
        async with connect_as(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.call_tool(
                    'call_operation', {'name': 'delete_product', 'arguments': {'productId': '1'}}
                )
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_legacy_client_receives_a_tool_error(self, gateway, upstream_requests):
        async with connect_as(gateway, READER, LEGACY) as session:
            result = await session.client.call_tool(
                'call_operation', {'name': 'delete_product', 'arguments': {'productId': '1'}}
            )
        assert result.is_error
        assert 'products:delete' in text_of(result)
        assert upstream_requests == []


class TestPromotedResources:
    """A resource promoted from an operation is listed and read only under the scopes that operation requires."""

    @pytest.fixture
    def gateway(self, make_gateway) -> Gateway:
        return make_gateway(promote_resources=True)

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    @pytest.mark.parametrize(
        ('client_id', 'resources', 'templates'),
        [(READER, ['shop://products'], ['shop://products/{productId}']), (WRITER, [], [])],
    )
    async def test_each_client_lists_only_what_its_scopes_open(self, gateway, mode, client_id, resources, templates):
        async with connect_as(gateway, client_id, mode) as session:
            listed = await session.client.list_resources()
            listed_templates = await session.client.list_resource_templates()
        assert [str(resource.uri) for resource in listed.resources] == resources
        assert [template.uri_template for template in listed_templates.resource_templates] == templates
        assert listed.model_dump(by_alias=True)['cacheScope'] == 'private'

    async def test_a_modern_client_is_challenged_over_http(self, gateway, upstream_requests):
        """``Mcp-Name`` carries the URI, which the template it expands from decides."""
        async with connect_as(gateway, STEPPER, MODERN) as session:
            with pytest.raises(MCPError):
                await session.client.read_resource(PRODUCT_RESOURCE)
        assert session.refusal.status_code == 403
        assert session.refusal.headers['www-authenticate'] == READ_CHALLENGE
        assert upstream_requests == []

    async def test_a_legacy_client_receives_a_protocol_error(self, gateway, upstream_requests):
        async with connect_as(gateway, WRITER, LEGACY) as session:
            with pytest.raises(MCPError, match='products:read'):
                await session.client.read_resource(PRODUCT_RESOURCE)
        assert upstream_requests == []

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_read_within_the_scopes_reaches_the_upstream(self, gateway, upstream_requests, mode):
        async with connect_as(gateway, READER, mode) as session:
            await session.client.read_resource(PRODUCT_RESOURCE)
        assert [(request.method, request.url.path) for request in upstream_requests] == [('GET', '/v1/products/7')]


class TestAuthorizationServer:
    """With pre-registered clients the gateway grants each its own scopes and advertises none up front."""

    @pytest.mark.parametrize(
        ('client_id', 'requested', 'granted'),
        [
            (DELETER, None, 'products:read products:delete'),
            (DELETER, 'products:delete products:write', 'products:delete'),
            (STEPPER, None, 'products:write'),
            (STEPPER, 'products:write products:delete', 'products:write products:delete'),
        ],
    )
    async def test_the_token_response_states_the_granted_scope(self, gateway, client_id, requested, granted):
        """Asking for nothing grants the client's defaults, and a request is narrowed to what the client may hold."""
        token = await sign_in_as(gateway, client_id, requested)
        assert token.scope == granted

    @pytest.mark.parametrize(
        ('client_id', 'granted'), [(DELETER, 'products:read products:delete'), (STEPPER, 'products:write')]
    )
    async def test_the_sdk_client_is_granted_the_clients_defaults(self, make_gateway, client_id, granted):
        """The SDK's own client names no scope on its first sign-in, so it starts on the client's defaults."""
        token = await sign_in_through_the_sdk(make_gateway, client_id)
        assert token.scope == granted

    async def test_a_request_for_nothing_the_client_may_hold_is_refused(self, gateway):
        with pytest.raises(InvalidScopeError):
            await sign_in_as(gateway, DELETER, 'products:write')

    def test_discovery_advertises_no_scopes_and_no_registration(self, gateway):
        """Discovery happens before the client is known, so no single scope set would be right."""
        http = TestClient(gateway.build_app())
        resource = http.get('/.well-known/oauth-protected-resource/shop').json()
        server = http.get('/.well-known/oauth-authorization-server/shop').json()
        assert 'scopes_supported' not in resource
        assert 'scopes_supported' not in server
        assert 'registration_endpoint' not in server
        assert http.post('/shop/register', json={'redirect_uris': ['http://localhost/cb']}).status_code in {
            httpx.codes.NOT_FOUND,
            httpx.codes.METHOD_NOT_ALLOWED,
        }
