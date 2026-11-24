import pytest
from mcp.server.auth.provider import AccessToken

from openapi_mcp_gateway.scopes import (
    McpAccessToken,
    UpstreamNoGrantSource,
    UpstreamRecordedGrantSource,
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

    def test_a_token_recording_no_upstream_grant_leaves_it_to_the_call(self):
        token = McpAccessToken(token='external', client_id='agent', scopes=['pets:read'], upstream_scopes=None)
        assert grant_of(token).upstream_scopes is None

    def test_a_token_no_gateway_verifier_returns_is_a_wiring_mistake(self):
        """Reading it as holding nothing would hide the mistake behind refusals."""
        with pytest.raises(TypeError, match='McpAccessToken'):
            grant_of(AccessToken(token='plain', client_id='agent', scopes=[]))


class TestUpstreamGrantSource:
    """Whether a user's own upstream grant is known, which ``enforce_scopes`` needs."""

    @pytest.mark.parametrize(
        ('grant_source', 'records'), [(UpstreamNoGrantSource(), False), (UpstreamRecordedGrantSource(), True)]
    )
    def test_each_source_states_whether_it_records_the_user_grant(self, grant_source, records):
        assert grant_source.records_user_grant is records

    async def test_a_call_outside_an_authenticated_request_reads_an_empty_grant(self):
        grant = await UpstreamRecordedGrantSource().read_call_grant()
        assert grant.mcp_scopes == frozenset()
