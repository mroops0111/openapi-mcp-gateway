import base64

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request

from openapi_mcp_gateway.auth.mcp.client import McpDynamicClientRegistry, McpPreRegisteredClientDirectory
from openapi_mcp_gateway.auth.mcp.client_credentials import (
    McpClientCredentialsError,
    McpClientCredentialsGrant,
    McpIssuedTokenVerifier,
    _read_client_credentials,
)
from openapi_mcp_gateway.auth.mcp.record_origin import McpRecordOrigin
from openapi_mcp_gateway.auth.mcp.token_issuer import McpTokenIssuer, McpTokenPolicy
from openapi_mcp_gateway.scopes import McpAccessToken
from openapi_mcp_gateway.settings import ClientConfig
from tests.constants import GATEWAY_ISSUER


@pytest.fixture
def tokens(memory_store) -> McpTokenIssuer:
    return McpTokenIssuer(memory_store, McpRecordOrigin(issuer=GATEWAY_ISSUER), McpTokenPolicy(scopes=[]))


@pytest.fixture
def grant(tokens) -> McpClientCredentialsGrant:
    clients = {'agent': ClientConfig(client_secret='secret', scopes=['pets:read'])}
    return McpClientCredentialsGrant(tokens, McpPreRegisteredClientDirectory(clients, []))


class TestGrant:
    """The grant authenticates an agent by its secret and issues it a token of its own scopes."""

    async def test_the_issued_token_verifies_as_an_mcp_token(self, grant, tokens):
        client = await grant.authenticate('agent', 'secret')
        token = await grant.exchange(client, None)

        verified = await McpIssuedTokenVerifier(tokens).verify_token(token.access_token)

        assert isinstance(verified, McpAccessToken)
        assert verified.scopes == ['pets:read']
        assert verified.mcp_grantable_scopes == ['pets:read']
        assert verified.upstream_scopes == []

    @pytest.mark.parametrize(('client_id', 'secret'), [('agent', 'wrong'), ('stranger', 'secret')])
    async def test_an_unknown_client_or_wrong_secret_is_refused(self, grant, client_id, secret):
        with pytest.raises(McpClientCredentialsError) as raised:
            await grant.authenticate(client_id, secret)
        assert raised.value.error == 'invalid_client'
        assert raised.value.status_code == 401

    async def test_a_self_registered_client_may_not_use_the_grant(self, tokens, memory_store):
        """Only an operator-declared agent has the secret that proves who it is."""
        grant = McpClientCredentialsGrant(tokens, McpDynamicClientRegistry(memory_store, GATEWAY_ISSUER))
        client = OAuthClientInformationFull(client_id='self-registered', redirect_uris=None)

        with pytest.raises(McpClientCredentialsError) as raised:
            await grant.exchange(client, None)
        assert raised.value.error == 'unauthorized_client'


class TestClientAuthentication:
    """A client presents its secret one way, by HTTP Basic or in the form."""

    def test_basic_credentials_are_form_decoded(self):
        header = 'Basic ' + base64.b64encode(b'my+agent:p%40ss').decode()
        assert _read_client_credentials(_request(header), {}) == ('my agent', 'p@ss')

    def test_two_methods_at_once_are_refused(self):
        header = 'Basic ' + base64.b64encode(b'agent:secret').decode()
        with pytest.raises(McpClientCredentialsError) as raised:
            _read_client_credentials(_request(header), {'client_secret': 'secret'})
        assert raised.value.error == 'invalid_request'

    def test_malformed_basic_credentials_are_refused(self):
        with pytest.raises(McpClientCredentialsError) as raised:
            _read_client_credentials(_request('Basic not-base64!'), {})
        assert raised.value.error == 'invalid_client'

    def test_a_basic_client_id_must_match_the_forms(self):
        header = 'Basic ' + base64.b64encode(b'agent:secret').decode()
        with pytest.raises(McpClientCredentialsError):
            _read_client_credentials(_request(header), {'client_id': 'other'})


def _request(authorization: str) -> Request:
    """A request carrying ``authorization`` and nothing else."""
    return Request({'type': 'http', 'headers': [(b'authorization', authorization.encode())]})
