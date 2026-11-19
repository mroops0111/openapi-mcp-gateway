import base64

import pytest
from starlette.requests import Request

from openapi_mcp_gateway.auth.mcp.client import McpPreRegisteredClient, McpPreRegisteredClientDirectory
from openapi_mcp_gateway.auth.mcp.client_credentials import (
    McpClientCredentialsError,
    McpClientCredentialsGrant,
    McpGatewayTokenVerifier,
    _read_client_credentials,
)
from openapi_mcp_gateway.auth.mcp.record_origin import McpRecordOrigin
from openapi_mcp_gateway.auth.mcp.token_issuer import McpTokenIssuer, McpTokenPolicy
from openapi_mcp_gateway.scopes import McpAccessToken
from tests.constants import GATEWAY_ISSUER


@pytest.fixture
def tokens(memory_store) -> McpTokenIssuer:
    return McpTokenIssuer(memory_store, McpRecordOrigin(issuer=GATEWAY_ISSUER), McpTokenPolicy(scopes=[]))


@pytest.fixture
def grant(tokens) -> McpClientCredentialsGrant:
    agent = McpPreRegisteredClient(
        client_id='agent',
        client_secret='secret',
        token_endpoint_auth_method='client_secret_basic',
        redirect_uris=None,
        scope='pets:read',
        default_scopes=['pets:read'],
    )
    return McpClientCredentialsGrant(tokens, McpPreRegisteredClientDirectory([agent]))


class TestGrant:
    """The grant authenticates an agent by its secret and issues it a token of its own scopes."""

    async def test_the_issued_token_verifies_as_an_mcp_token(self, grant, tokens):
        client = await grant.authenticate('agent', 'secret')
        token = await grant.exchange(client, None)

        verified = await McpGatewayTokenVerifier(tokens, grant.clients).verify_token(token.access_token)

        assert isinstance(verified, McpAccessToken)
        assert verified.scopes == ['pets:read']
        assert verified.mcp_grantable_scopes == ['pets:read']
        assert verified.upstream_scopes == []

    async def test_a_token_of_a_removed_client_is_refused(self, grant, tokens):
        """Removing an agent from the config revokes the tokens it already holds, even ones a shared store kept."""
        token = await grant.exchange(await grant.authenticate('agent', 'secret'), None)

        verifier = McpGatewayTokenVerifier(tokens, McpPreRegisteredClientDirectory([]))

        assert await verifier.verify_token(token.access_token) is None

    @pytest.mark.parametrize(('client_id', 'secret'), [('agent', 'wrong'), ('stranger', 'secret')])
    async def test_an_unknown_client_or_wrong_secret_is_refused(self, grant, client_id, secret):
        with pytest.raises(McpClientCredentialsError) as raised:
            await grant.authenticate(client_id, secret)
        assert raised.value.error == 'invalid_client'
        assert raised.value.status_code == 401


class TestClientAuthentication:
    """A client presents its secret one way, by HTTP Basic or in the form."""

    def test_basic_credentials_are_percent_decoded(self):
        header = 'Basic ' + base64.b64encode(b'my%20agent:p%40ss').decode()
        assert _read_client_credentials(_build_request(header), {}) == ('my agent', 'p@ss')

    def test_a_raw_plus_in_a_secret_stays_a_plus(self):
        """``curl -u`` sends a secret unencoded, and a base64 secret often holds a ``+``."""
        header = 'Basic ' + base64.b64encode(b'agent:ab+cd').decode()
        assert _read_client_credentials(_build_request(header), {}) == ('agent', 'ab+cd')

    def test_the_scheme_name_is_matched_in_any_case(self):
        header = 'basic ' + base64.b64encode(b'agent:secret').decode()
        assert _read_client_credentials(_build_request(header), {}) == ('agent', 'secret')

    def test_two_methods_at_once_are_refused(self):
        header = 'Basic ' + base64.b64encode(b'agent:secret').decode()
        with pytest.raises(McpClientCredentialsError) as raised:
            _read_client_credentials(_build_request(header), {'client_secret': 'secret'})
        assert raised.value.error == 'invalid_request'

    def test_malformed_basic_credentials_are_refused(self):
        with pytest.raises(McpClientCredentialsError) as raised:
            _read_client_credentials(_build_request('Basic not-base64!'), {})
        assert raised.value.error == 'invalid_client'

    def test_a_basic_client_id_must_match_the_forms(self):
        header = 'Basic ' + base64.b64encode(b'agent:secret').decode()
        with pytest.raises(McpClientCredentialsError):
            _read_client_credentials(_build_request(header), {'client_id': 'other'})


def _build_request(authorization: str) -> Request:
    """A request carrying ``authorization`` and nothing else."""
    return Request({'type': 'http', 'headers': [(b'authorization', authorization.encode())]})
