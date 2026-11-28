import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from openapi_mcp_gateway.auth.mcp.record_origin import McpRecordOrigin
from openapi_mcp_gateway.auth.mcp.token_issuer import (
    McpTokenAttachment,
    McpTokenIssuer,
    McpTokenPolicy,
)
from tests.constants import GATEWAY_ISSUER, GATEWAY_RESOURCE


# A client registered with the scopes its tokens may step up to.
CLIENT = OAuthClientInformationFull(client_id='agent', redirect_uris=None, scope='products:read products:write')


@pytest.fixture
def tokens(memory_store) -> McpTokenIssuer:
    return McpTokenIssuer(
        memory_store, McpRecordOrigin(issuer=GATEWAY_ISSUER), McpTokenPolicy(scopes=[]), GATEWAY_RESOURCE
    )


class TestIssuing:
    """An access token alone serves an agent, and a sign-in also gets a refresh token paired with it."""

    async def test_an_access_token_alone_has_no_refresh_token(self, tokens, memory_store):
        token = await tokens.issue_access_token(CLIENT, ['products:read'], McpTokenAttachment())

        assert token.refresh_token is None
        assert token.scope == 'products:read'
        loaded = await tokens.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.mcp_grantable_scopes == ['products:read', 'products:write']
        assert await memory_store.get_mapping('mcp_access_token', token.access_token, 'mcp_refresh_token') is None

    async def test_issued_tokens_pair_the_access_token_with_a_refresh_token(self, tokens, memory_store):
        token = await tokens.issue_tokens(CLIENT, ['products:read'], McpTokenAttachment())

        assert token.refresh_token is not None
        paired = await memory_store.get_mapping('mcp_access_token', token.access_token, 'mcp_refresh_token')
        assert paired == token.refresh_token

    async def test_an_access_token_never_outlives_what_it_links(self, tokens):
        attachment = McpTokenAttachment(links_expire_at=int(time.time()) + 30)

        token = await tokens.issue_access_token(CLIENT, ['products:read'], attachment)

        assert token.expires_in is not None
        assert token.expires_in <= 30


class TestServesResource:
    """A requested RFC 8707 resource matches this endpoint as a URL, not as a string."""

    @pytest.mark.parametrize(
        'requested',
        [
            None,
            GATEWAY_RESOURCE,
            f'{GATEWAY_RESOURCE}/',
            GATEWAY_RESOURCE.replace('mcp.example.com', 'MCP.Example.com:443'),
        ],
    )
    def test_this_endpoint_is_served(self, tokens, requested):
        assert tokens.serves_resource(requested)

    @pytest.mark.parametrize('requested', ['https://elsewhere.example.com/mcp', f'{GATEWAY_ISSUER}', 'not a url'])
    def test_anything_else_is_not(self, tokens, requested):
        assert not tokens.serves_resource(requested)
