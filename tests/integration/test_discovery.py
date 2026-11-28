import pathlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    GatewayConfig,
)
from tests.constants import (
    GATEWAY_URL,
)
from tests.oauth import authorization_code_gateway, shared_credential_server


class TestWellKnownNoOAuth:
    """Well-known endpoints return 404 when the server has no OAuth or is unknown."""

    @pytest.mark.parametrize(
        'path',
        [
            '/.well-known/oauth-authorization-server/shop',
            '/.well-known/oauth-protected-resource/shop',
            '/.well-known/oauth-authorization-server/unknown',
        ],
    )
    def test_returns_404(self, client, path):
        """Both endpoint variants and unknown server names yield 404."""
        response = client.get(path)
        assert response.status_code == 404


class TestWellKnownOAuth:
    """Well-known endpoints for an OAuth-enabled server."""

    @pytest.fixture
    def oauth_client(self, oauth_gateway):
        """Test client over the OAuth2 gateway."""
        return TestClient(oauth_gateway.build_app())

    def test_authorization_server_metadata(self, oauth_client):
        """OAuth metadata document advertises issuer, endpoints and PKCE method."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/shop')
        assert response.status_code == 200
        data = response.json()
        assert 'shop' in data['issuer']
        assert data['authorization_endpoint'].endswith('/authorize')
        assert data['token_endpoint'].endswith('/token')
        assert 'S256' in data['code_challenge_methods_supported']

    @pytest.mark.parametrize(
        'path',
        ['/.well-known/oauth-authorization-server/shop', '/shop/.well-known/oauth-authorization-server'],
    )
    def test_metadata_advertises_the_iss_parameter(self, oauth_client, path):
        """Both metadata locations promise ``iss`` on authorization responses, which the provider sends (RFC 9207)."""
        response = oauth_client.get(path)
        assert response.status_code == 200
        assert response.json()['authorization_response_iss_parameter_supported'] is True

    def test_metadata_documents_are_announced_while_unlisted_clients_may_sign_in(self, oauth_client):
        """The MCP spec has a client prefer its metadata document over registering where this is announced."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/shop')
        assert response.json()['client_id_metadata_document_supported'] is True

    def test_a_closed_set_of_plain_clients_announces_no_metadata_documents(self, shop_json_path):
        gateway = authorization_code_gateway(shop_json_path, {'agent': {}})
        response = TestClient(gateway.build_app()).get('/.well-known/oauth-authorization-server/shop')
        assert 'client_id_metadata_document_supported' not in response.json()

    def test_authorization_server_with_mcp(self, oauth_client):
        """Suffixing ``/mcp`` on the metadata path is also served."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/shop/mcp')
        assert response.status_code == 200

    def test_protected_resource_metadata(self, oauth_client):
        """Protected-resource metadata points to the MCP endpoint and authorization servers."""
        response = oauth_client.get('/.well-known/oauth-protected-resource/shop')
        assert response.status_code == 200
        data = response.json()
        assert data['resource'].endswith('/mcp')
        assert len(data['authorization_servers']) == 1

    def test_options_cors(self, oauth_client):
        """``OPTIONS`` on the metadata endpoint returns 200 for CORS preflight."""
        response = oauth_client.options('/.well-known/oauth-authorization-server/shop')
        assert response.status_code == 200


class TestWellKnownUnderPathPrefix:
    """Discovery details an MCP SDK client cannot show, which the sign-in tests under ``authorization/`` cover end to end."""

    def test_the_published_urls_are_normalized(self, shop_json_path):
        """An upper-case host and a default port in ``url`` are published as a client would echo them."""
        http = TestClient(_shared_credential_gateway(shop_json_path, 'https://MCP.Example.com:443', 'products'))

        resource = http.get('/.well-known/oauth-protected-resource/products/mcp').json()
        assert resource['resource'] == f'{GATEWAY_URL}/products/mcp'
        assert resource['authorization_servers'] == [f'{GATEWAY_URL}/products']
        token = http.post(
            '/products/token',
            data={'grant_type': 'client_credentials', 'resource': f'{GATEWAY_URL}/products/mcp'},
            auth=('agent', 'agent-secret'),
        )
        assert token.status_code == 200, token.text

    def test_the_server_name_still_finds_the_documents(self, shop_json_path):
        """The URLs served before discovery followed the mount path keep working."""
        http = TestClient(_shared_credential_gateway(shop_json_path, GATEWAY_URL, 'products'))

        assert http.get('/.well-known/oauth-authorization-server/shop').status_code == 200
        assert http.get('/.well-known/oauth-protected-resource/shop/mcp').status_code == 200


def _shared_credential_gateway(spec_path: pathlib.Path, gateway_url: str, path_prefix: str) -> FastAPI:
    """The app of a gateway at ``gateway_url`` issuing MCP tokens for one bearer-token server under ``path_prefix``."""
    server = shared_credential_server(spec_path, 'shop', path_prefix)
    gateway = Gateway.from_config(GatewayConfig.model_validate({'url': gateway_url, 'servers': [server]}))
    return gateway.build_app()
