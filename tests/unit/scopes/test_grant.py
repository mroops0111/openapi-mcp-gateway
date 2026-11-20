from openapi_mcp_gateway.scopes import (
    McpAccessToken,
    grant_of,
)


class TestTokenGrant:
    """What a token holds, read from the token on the request."""

    def test_a_token_minted_before_recording_grantable_scopes_leaves_them_unknown(self):
        """Its challenge stays whole, so the client can still step up, while lists show only what it holds."""
        token = McpAccessToken(token='mcp_access', client_id='stepper', scopes=['pets:read'])
        grant = grant_of(token)
        assert grant.mcp_grantable_scopes is None
        assert grant.mcp_reachable_scopes == {'pets:read'}

    def test_a_recorded_token_reaches_what_its_client_may_be_granted(self):
        token = McpAccessToken(
            token='mcp_access',
            client_id='stepper',
            scopes=['pets:read'],
            mcp_grantable_scopes=['pets:read', 'pets:write'],
        )
        assert grant_of(token).mcp_reachable_scopes == {'pets:read', 'pets:write'}
