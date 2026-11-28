import logging

import pytest
from mcp import MCPError

from tests.integration.scopes.clients import (
    DELETE_CHALLENGE,
    DELETER,
    LEGACY,
    MODERN,
    READ_TOOLS,
    READER,
    STEPPER,
    WRITER,
    connect_as,
    listed_tool_names,
    sign_in_with_the_sdk,
    step_up_with_the_sdk,
)
from tests.oauth import query_value
from tests.scope import DELETE_PRODUCT_UPSTREAM_REFUSAL
from tests.tool_result import text_of


class TestUpstreamGrant:
    """With ``enforce_scopes``, an operation also needs the upstream scopes its ``security`` declares."""

    @pytest.mark.parametrize(
        ('upstream_scope', 'expected'),
        [('read:products', READ_TOOLS), ('read:products write:products', [*READ_TOOLS, 'delete_product'])],
    )
    async def test_the_list_shows_only_what_the_grant_opens(self, make_enforcing_gateway, upstream_scope, expected):
        async with connect_as(make_enforcing_gateway(), DELETER, MODERN, upstream_scope) as session:
            assert await listed_tool_names(session) == expected

    @pytest.mark.parametrize(
        ('style', 'tool', 'arguments'),
        [
            ('static', 'delete_product', {'productId': '1'}),
            ('dynamic', 'call_operation', {'name': 'delete_product', 'arguments': {'productId': '1'}}),
        ],
    )
    async def test_a_short_grant_is_a_tool_error_rather_than_a_challenge(
        self, make_enforcing_gateway, upstream_requests, style, tool, arguments
    ):
        """The client already holds the MCP scopes, so stepping up cannot change what the upstream granted."""
        async with connect_as(make_enforcing_gateway(style), DELETER, MODERN, 'read:products') as session:
            result = await session.client.call_tool(tool, arguments)
        assert result.is_error
        # The meta-tool's refusal arrives prefixed by the SDK, as any error a tool raises does.
        assert DELETE_PRODUCT_UPSTREAM_REFUSAL in text_of(result)
        assert upstream_requests == []

    async def test_missing_mcp_scopes_are_challenged_first(self, make_enforcing_gateway):
        """A client steps up for the MCP scopes, so it hears about them before the upstream grant."""
        async with connect_as(make_enforcing_gateway(), STEPPER, MODERN, 'read:products') as session:
            with pytest.raises(MCPError):
                await session.client.call_tool('delete_product', {'productId': '1'})
        assert session.refusal.headers['www-authenticate'] == DELETE_CHALLENGE

    async def test_a_tool_the_client_may_step_up_for_stays_listed_before_the_upstream_grants_it(
        self, make_enforcing_gateway
    ):
        """The sign-in a step-up starts asks the upstream for what ``delete_product`` needs, so hiding it would stall."""
        async with connect_as(make_enforcing_gateway(), STEPPER, MODERN, 'write:products') as session:
            assert 'delete_product' in await listed_tool_names(session)

    @pytest.mark.parametrize('mode', [MODERN, LEGACY])
    async def test_a_tool_no_sign_in_can_open_is_not_exposed(self, make_enforcing_gateway, caplog, mode):
        """No sign-in within ``read:products`` opens ``create_product`` or ``delete_product``, so a step-up would stall."""
        with caplog.at_level(logging.WARNING):
            gateway = make_enforcing_gateway(scopes=['read:products'])
        assert 'no sign-in can open 2 operation(s)' in caplog.text
        async with connect_as(gateway, STEPPER, mode, 'read:products') as session:
            names = await listed_tool_names(session)
        assert 'list_products' in names
        assert not {'create_product', 'delete_product'} & set(names)

    def test_startup_warns_about_operations_the_requested_scopes_cannot_open(self, make_gateway, secured_spec, caplog):
        """Without ``enforce_scopes`` the gateway does not check the grant, so the call may still succeed."""
        with caplog.at_level(logging.WARNING):
            make_gateway(spec=secured_spec, upstream={'scopes': ['read:products']})
        assert '"deleter" can reach 1 operation(s) the requested upstream scopes cannot open' in caplog.text
        assert '"reader"' not in caplog.text


class TestUpstreamDownscoping:
    """Each sign-in asks the upstream for what the operations its MCP scopes open declare, within the ceiling."""

    @pytest.mark.parametrize(
        ('client_id', 'requested'),
        [(READER, 'read:products'), (WRITER, 'write:products'), (DELETER, 'read:products write:products')],
    )
    @pytest.mark.parametrize('ceiling', [['read:products', 'write:products'], []], ids=['listed', 'declared'])
    async def test_a_sign_in_asks_for_what_its_mcp_scopes_need(
        self, make_enforcing_gateway, client_id, requested, ceiling
    ):
        """Without ``auth.upstream.scopes``, the ceiling is every scope the operations declare."""
        browser = await sign_in_with_the_sdk(lambda url: make_enforcing_gateway(url=url, scopes=ceiling), client_id)
        assert [query_value(url, 'scope') for url in browser.upstream_sign_ins] == [requested]

    async def test_a_required_scope_is_asked_for_at_every_sign_in(self, make_enforcing_gateway):
        browser = await sign_in_with_the_sdk(
            lambda url: make_enforcing_gateway(url=url, required_scopes=['offline_access']), READER
        )
        assert query_value(browser.upstream_sign_ins[0], 'scope') == 'offline_access read:products'

    async def test_without_enforce_scopes_every_sign_in_asks_for_the_whole_list(self, make_gateway, secured_spec):
        upstream = {'scopes': ['read:products', 'write:products']}
        browser = await sign_in_with_the_sdk(
            lambda url: make_gateway(spec=secured_spec, upstream=upstream, url=url), READER
        )
        assert query_value(browser.upstream_sign_ins[0], 'scope') == 'read:products write:products'

    async def test_a_step_up_asks_the_upstream_for_what_the_wider_grant_needs(
        self, make_enforcing_gateway, upstream_requests
    ):
        """The consent page marks the upstream scope the step-up adds, and the call then reaches the upstream."""
        browser, result = await step_up_with_the_sdk(
            lambda url: make_enforcing_gateway(url=url), STEPPER, 'delete_product', {'productId': '1'}
        )
        assert not result.is_error
        assert [query_value(url, 'scope') for url in browser.upstream_sign_ins] == [
            'write:products',
            'read:products write:products',
        ]
        assert 'read:products</code><span class="new">New</span>' in browser.consent_pages[-1]
        assert [request.method for request in upstream_requests] == ['DELETE']
