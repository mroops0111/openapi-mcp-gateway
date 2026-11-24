import pathlib
import time
from unittest.mock import MagicMock

import jwt
import pytest
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    McpAuthConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from tests.constants import (
    API_URL,
    GATEWAY_URL,
    ISSUER,
)
from tests.flow import discovered_external_issuer


class TestTokenExchangeDiscovery:
    """Discovery documents for a server whose authorization is delegated to an external issuer."""

    @pytest.fixture
    def delegating_client(self, petstore_json_path):
        """Gateway whose petstore server validates tokens from an issuer it does not own."""
        return _delegating_client(petstore_json_path)

    def test_protected_resource_names_the_external_issuer(self, delegating_client):
        """The document points clients at the issuer, and names this endpoint as the resource."""
        response = delegating_client.get('/.well-known/oauth-protected-resource/petstore')

        assert response.status_code == 200
        data = response.json()
        assert data['resource'] == f'{GATEWAY_URL}/petstore/mcp'
        assert data['authorization_servers'] == [ISSUER]

    def test_gateway_does_not_claim_to_be_an_authorization_server(self, delegating_client):
        """The AS metadata path 404s, since the gateway serves no /authorize or /token here.

        Publishing a document would send clients to endpoints this app does not have.
        """
        response = delegating_client.get('/.well-known/oauth-authorization-server/petstore')

        assert response.status_code == 404
        assert 'not an authorization server' in response.json()['error']

    def test_no_oauth_endpoints_are_mounted(self, delegating_client):
        """``/authorize`` and ``/token`` under the mount path belong to the issuer, not the gateway."""
        assert delegating_client.get('/petstore/authorize').status_code == 404
        assert delegating_client.post('/petstore/token').status_code == 404

    def test_healthz_reports_the_endpoint_as_protected(self, delegating_client):
        """A delegating server is still OAuth-protected, so health must not report it as open."""
        servers = delegating_client.get('/healthz').json()['servers']

        assert servers[0]['auth'] == 'oauth2'


class TestRejectedTokenResponse:
    """A token the gateway refuses produces a 401 challenge, not a server error.

    The MCP SDK's bearer backend does not guard the ``verify_token`` call,
    so an exception raised there reaches uvicorn as a 500.
    A client reads that as the server being broken and keeps resending the same credential,
    where a 401 would have sent it to re-authorize.
    """

    RESOURCE = f'{GATEWAY_URL}/petstore/mcp'

    @pytest.fixture
    def delegating_app(self, petstore_json_path, signing_key):
        """Gateway delegating to an external issuer, with that issuer's key resolvable."""
        jwk = MagicMock()
        jwk.key = signing_key.public_key()
        jwk.key_type = 'RSA'
        jwk_client = MagicMock()
        jwk_client.get_signing_key_from_jwt.return_value = jwk
        return _delegating_client(petstore_json_path, jwk_client)

    def _token(self, signing_key, **claims) -> str:
        """A token signed by the issuer's key, where a claim set to ``None`` is left out."""
        payload = {'iss': ISSUER, 'aud': self.RESOURCE, 'sub': 'user-1', 'exp': int(time.time()) + 300, **claims}
        return jwt.encode({k: v for k, v in payload.items() if v is not None}, signing_key, algorithm='RS256')

    def _post(self, client, token: str):
        return client.post(
            '/petstore/mcp',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json',
            },
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        )

    def test_expired_token_is_a_challenge_not_a_server_error(self, delegating_app, signing_key):
        """Expiry is the ordinary path here, since the issuer owns the lifetimes.

        Keycloak defaults to five minutes, so every session reaches this within minutes of starting.
        A 500 leaves the client resending the same dead token forever.
        """
        expired = self._token(signing_key, exp=int(time.time()) - 60)

        response = self._post(delegating_app, expired)

        assert response.status_code == 401
        assert 'invalid_token' in response.headers.get('WWW-Authenticate', '')

    @pytest.mark.parametrize(
        ('claims', 'reason'),
        [
            ({'aud': API_URL}, 'audience naming the upstream instead of this endpoint'),
            ({'iss': 'https://other.example.com'}, 'another issuer'),
            ({'aud': None}, 'no audience at all'),
        ],
    )
    def test_every_rejection_reason_is_a_challenge(self, delegating_app, signing_key, claims, reason):
        """No rejection path may reach the client as a server error."""
        token = self._token(signing_key, **claims)

        response = self._post(delegating_app, token)

        assert response.status_code == 401, f'rejected for {reason}'
        assert 'invalid_token' in response.headers.get('WWW-Authenticate', '')

    def test_a_credential_that_is_not_a_jwt_is_also_a_challenge(self, delegating_app):
        """An opaque credential is unrecognised rather than invalid, and still must not 500."""
        response = self._post(delegating_app, 'an-opaque-session-token')

        assert response.status_code == 401


def _delegating_client(spec_path: pathlib.Path, jwk_client: MagicMock | None = None) -> TestClient:
    """Test client over a petstore server that validates tokens from ``ISSUER``, an issuer it does not own."""
    config = GatewayConfig(
        url=GATEWAY_URL,
        servers=[
            ServerConfig(
                name='petstore',
                spec=str(spec_path),
                auth=AuthConfig(
                    upstream=UpstreamAuthConfig(
                        type='oauth2',
                        flow='token_exchange',
                        audience=API_URL,
                        client_id='gateway',
                        client_secret='secret',
                        scopes=['read'],
                    ),
                    mcp=McpAuthConfig(issuer=ISSUER, required_scopes=['mcp']),
                ),
            ),
        ],
    )
    with discovered_external_issuer(jwk_client):
        gateway = Gateway.from_config(config)
        return TestClient(gateway._build_app(transport='streamable-http'))
