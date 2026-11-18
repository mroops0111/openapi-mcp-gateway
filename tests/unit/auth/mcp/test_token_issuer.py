import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from openapi_mcp_gateway.auth.mcp.record_origin import McpRecordOrigin
from openapi_mcp_gateway.auth.mcp.token_issuer import (
    MCP_DEFAULT_SCOPE,
    McpTokenAttachment,
    McpTokenIssuer,
    McpTokenPolicy,
)
from openapi_mcp_gateway.settings import McpAuthConfig
from tests.constants import GATEWAY_ISSUER


# A client registered with the scopes its tokens may step up to.
CLIENT = OAuthClientInformationFull(client_id='agent', redirect_uris=None, scope='pets:read pets:write')


@pytest.fixture
def tokens(memory_store) -> McpTokenIssuer:
    return McpTokenIssuer(memory_store, McpRecordOrigin(issuer=GATEWAY_ISSUER), McpTokenPolicy(scopes=[]))


class TestPolicyFromConfig:
    """``auth.mcp`` decides the endpoint scopes and lifetimes of every token the gateway mints."""

    def test_an_unset_endpoint_scope_falls_back_to_the_default(self):
        assert McpTokenPolicy.from_config(McpAuthConfig()).scopes == [MCP_DEFAULT_SCOPE]

    def test_required_scopes_are_carried_on_every_token(self):
        assert McpTokenPolicy.from_config(McpAuthConfig(required_scopes=['read'])).scopes == ['read']

    def test_operation_scopes_leave_no_endpoint_scope(self):
        """Each operation names its own scopes, so no endpoint-wide one is granted."""
        config = McpAuthConfig.model_validate(
            {'operation_scopes': {'auto': 'tags'}, 'clients': {'agent': {'scopes': ['pets:read']}}}
        )
        assert McpTokenPolicy.from_config(config).scopes == []

    def test_lifetimes_come_from_the_config(self):
        policy = McpTokenPolicy.from_config(McpAuthConfig(access_token_ttl=60, refresh_token_ttl=120))
        assert (policy.access_token_ttl, policy.refresh_token_ttl) == (60, 120)


class TestIssuing:
    """An access token alone serves an agent, and a sign-in also gets a refresh token paired with it."""

    async def test_an_access_token_alone_has_no_refresh_token(self, tokens, memory_store):
        token = await tokens.issue_access_token(CLIENT, ['pets:read'], McpTokenAttachment())

        assert token.refresh_token is None
        assert token.scope == 'pets:read'
        loaded = await tokens.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.mcp_grantable_scopes == ['pets:read', 'pets:write']
        assert await memory_store.get_mapping('mcp_access_token', token.access_token, 'mcp_refresh_token') is None

    async def test_issued_tokens_pair_the_access_token_with_a_refresh_token(self, tokens, memory_store):
        token = await tokens.issue_tokens(CLIENT, ['pets:read'], McpTokenAttachment())

        assert token.refresh_token is not None
        paired = await memory_store.get_mapping('mcp_access_token', token.access_token, 'mcp_refresh_token')
        assert paired == token.refresh_token

    async def test_an_access_token_never_outlives_what_it_links(self, tokens):
        attachment = McpTokenAttachment(links_expire_at=int(time.time()) + 30)

        token = await tokens.issue_access_token(CLIENT, ['pets:read'], attachment)

        assert token.expires_in is not None
        assert token.expires_in <= 30
