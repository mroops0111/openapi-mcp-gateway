import re

import pytest

from openapi_mcp_gateway.auth.mcp.client import McpClientMetadataDirectory
from openapi_mcp_gateway.auth.mcp.token_issuer import MCP_DEFAULT_SCOPE
from openapi_mcp_gateway.auth.settings_mapping import (
    build_mcp_client_directory,
    build_mcp_client_metadata_directory,
    build_mcp_other_clients,
    build_mcp_token_policy,
)
from openapi_mcp_gateway.settings import ClientConfig, McpAuthConfig, OperationScopesConfig, OtherClientsConfig
from tests.client_metadata import CLIENT_METADATA_URL, client_metadata_document, serving_resolver


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


class TestClientMetadataDirectory:
    """Keys of ``auth.mcp.clients`` that are URLs become Client ID Metadata Document clients."""

    @pytest.mark.parametrize(
        ('client', 'refusal'),
        [
            ({'client_secret': 'shh'}, r'client_secret cannot be set, since the CIMD draft forbids'),
            ({'redirect_uris': ['http://localhost/cb']}, r'redirect_uris cannot be set, since the client lists them'),
        ],
    )
    def test_a_metadata_document_client_takes_no_secret_or_redirects(self, client, refusal):
        """Its document is where both live, and the CIMD draft forbids a shared secret."""
        with pytest.raises(ValueError, match=rf'auth\.mcp\.clients\.{re.escape(CLIENT_METADATA_URL)}\.{refusal}'):
            _build_metadata_directory({CLIENT_METADATA_URL: ClientConfig.model_validate(client)})

    def test_a_metadata_document_key_is_recognised_whatever_the_case_of_its_scheme(self):
        """The directory reads the key as a document client either way, so its settings are checked the same."""
        client_id = CLIENT_METADATA_URL.replace('https://', 'HTTPS://')
        with pytest.raises(ValueError, match='client_secret cannot be set'):
            _build_metadata_directory({client_id: ClientConfig(client_secret='shh')})

    @pytest.mark.parametrize(
        'client_id', ['https://app.example.com', 'http://app.example.com/client.json', 'HTTP://app.example.com/c.json']
    )
    def test_a_url_no_metadata_document_can_sit_at_is_refused(self, client_id):
        with pytest.raises(ValueError, match='is not a Client ID Metadata Document URL'):
            _build_metadata_directory({client_id: ClientConfig()})

    async def test_a_metadata_document_client_keeps_its_scopes_and_consent(self):
        directory = _build_metadata_directory(
            {CLIENT_METADATA_URL: ClientConfig(scopes=['pets:write'], consent=False)}, ('api',)
        )
        client = await directory.get(CLIENT_METADATA_URL)
        assert client is not None
        assert (client.scope, client.token_endpoint_auth_method) == ('api pets:write', 'none')
        assert directory.requires_consent(CLIENT_METADATA_URL) is False

    async def test_a_metadata_document_client_is_left_out_of_the_plain_directory(self):
        """Each directory holds its own kind, so a URL key is never looked up as a plain client_id."""
        directory = build_mcp_client_directory({CLIENT_METADATA_URL: ClientConfig()}, ())
        assert await directory.get(CLIENT_METADATA_URL) is None


async def _build_record(client: dict, mcp_endpoint_scopes: tuple[str, ...] = ()):
    """The record a directory built from one ``auth.mcp.clients`` entry, ``agent``, gives the SDK."""
    directory = build_mcp_client_directory({'agent': ClientConfig.model_validate(client)}, mcp_endpoint_scopes)
    record = await directory.get('agent')
    assert record is not None
    return record


class TestOtherClients:
    """``auth.mcp.other_clients`` becomes the rule unlisted metadata document clients are admitted and granted by."""

    def test_its_scopes_are_granted_beside_the_endpoint_scopes(self):
        rule = build_mcp_other_clients(
            OtherClientsConfig(scopes=['pets:read', 'pets:write'], default_scopes=['pets:read']), ('api',)
        )
        client = rule.build_client(CLIENT_METADATA_URL)
        assert client.scope == 'api pets:read pets:write'
        assert client.validate_scope(None) == ['api', 'pets:read']

    @pytest.mark.parametrize(
        ('trusted_hosts', 'admitted'), [(None, True), (['App.Example.com'], True), (['example.com'], False)]
    )
    def test_trusted_hosts_match_the_document_host_exactly(self, trusted_hosts, admitted):
        """A parent domain does not admit its subdomains, so trusting one host never trusts a sibling."""
        rule = build_mcp_other_clients(OtherClientsConfig(trusted_hosts=trusted_hosts), ())
        assert rule.admits(CLIENT_METADATA_URL) is admitted

    def test_an_id_that_is_no_metadata_document_url_is_never_admitted(self):
        assert build_mcp_other_clients(OtherClientsConfig(), ()).admits('agent') is False


def _build_metadata_directory(
    clients: dict[str, ClientConfig], mcp_endpoint_scopes: tuple[str, ...] = ()
) -> McpClientMetadataDirectory:
    """The metadata document directory ``clients`` makes, whose network serves the document of ``CLIENT_METADATA_URL``."""
    config = McpAuthConfig(clients=clients, operation_scopes=OperationScopesConfig(auto='tags'))
    resolver = serving_resolver({CLIENT_METADATA_URL: client_metadata_document()})
    return build_mcp_client_metadata_directory(config, mcp_endpoint_scopes, resolver)
