import pytest

from openapi_mcp_gateway.auth.mcp.token_issuer import MCP_DEFAULT_SCOPE
from openapi_mcp_gateway.auth.settings_mapping import build_mcp_client_directory, build_mcp_token_policy
from openapi_mcp_gateway.settings import ClientConfig, McpAuthConfig


class TestTokenPolicy:
    """``auth.mcp`` decides the endpoint scopes and lifetimes of every token the gateway mints."""

    def test_an_unset_endpoint_scope_falls_back_to_the_default(self):
        assert build_mcp_token_policy(McpAuthConfig()).scopes == [MCP_DEFAULT_SCOPE]

    def test_required_scopes_are_carried_on_every_token(self):
        assert build_mcp_token_policy(McpAuthConfig(required_scopes=['read'])).scopes == ['read']

    def test_operation_scopes_leave_no_endpoint_scope(self):
        """Each operation names its own scopes, so no endpoint-wide one is granted."""
        config = McpAuthConfig.model_validate(
            {'operation_scopes': {'auto': 'tags'}, 'clients': {'agent': {'scopes': ['pets:read']}}}
        )
        assert build_mcp_token_policy(config).scopes == []

    def test_lifetimes_come_from_the_config(self):
        policy = build_mcp_token_policy(McpAuthConfig(access_token_ttl=60, refresh_token_ttl=120))
        assert (policy.access_token_ttl, policy.refresh_token_ttl) == (60, 120)


class TestClientDirectory:
    """Each ``auth.mcp.clients`` entry becomes the record the SDK authenticates and grants against."""

    @pytest.mark.parametrize(
        ('client', 'method'),
        [({'client_secret': 'shh'}, 'client_secret_basic'), ({}, 'none')],
    )
    async def test_a_secret_selects_basic_authentication(self, client, method):
        """Without a secret the client is public and held to PKCE alone."""
        record = await _build_record(client)
        assert record.token_endpoint_auth_method == method

    def test_a_secret_that_resolves_to_nothing_is_refused(self, monkeypatch):
        """An unset variable would otherwise turn a confidential client public, open to anyone knowing its id."""
        monkeypatch.delenv('AGENT_SECRET', raising=False)
        with pytest.raises(ValueError, match=r'auth\.mcp\.clients\.agent\.client_secret'):
            build_mcp_client_directory({'agent': ClientConfig(client_secret='${AGENT_SECRET}')}, ())

    async def test_endpoint_scopes_are_granted_beside_the_client_set(self):
        """Without ``auth.mcp.operation_scopes`` every token carries the endpoint scopes.

        A pre-registered client is granted them as a registered client would be.
        """
        record = await _build_record({}, mcp_endpoint_scopes=('api',))
        assert record.scope == 'api'
        assert record.validate_scope(None) == ['api']

    @pytest.mark.parametrize(('client', 'required'), [({}, True), ({'consent': False}, False)])
    def test_consent_follows_the_setting(self, client, required):
        directory = build_mcp_client_directory({'agent': ClientConfig.model_validate(client)}, ())
        assert directory.requires_consent('agent') is required


async def _build_record(client: dict, mcp_endpoint_scopes: tuple[str, ...] = ()):
    """The record a directory built from one ``auth.mcp.clients`` entry, ``agent``, gives the SDK."""
    directory = build_mcp_client_directory({'agent': ClientConfig.model_validate(client)}, mcp_endpoint_scopes)
    record = await directory.get('agent')
    assert record is not None
    return record
