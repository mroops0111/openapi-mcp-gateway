import contextlib
import pathlib
import typing

import anyio
import pytest
from fastapi import FastAPI
from mcp import Client
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from tests.constants import (
    AUTHORIZE_URL,
    GATEWAY_URL,
    TOKEN_URL,
)
from tests.oauth import authorization_code_gateway, shared_credential_server
from tests.serving import serve


@pytest.fixture
def oauth_gateway(petstore_json_path):
    """Gateway whose petstore server uses the OAuth2 authorization-code flow."""
    config = GatewayConfig(
        url=GATEWAY_URL,
        servers=[
            ServerConfig(
                name='petstore',
                spec=str(petstore_json_path),
                auth=AuthConfig(
                    upstream=UpstreamAuthConfig(
                        type='oauth2',
                        client_id='test-client-id',
                        client_secret='test-client-secret',
                        authorization_url=AUTHORIZE_URL,
                        token_url=TOKEN_URL,
                        scopes=['read'],
                    )
                ),
            ),
        ],
    )
    return Gateway.from_config(config)


class TestWellKnownNoOAuth:
    """Well-known endpoints return 404 when the server has no OAuth or is unknown."""

    @pytest.mark.parametrize(
        'path',
        [
            '/.well-known/oauth-authorization-server/petstore',
            '/.well-known/oauth-protected-resource/petstore',
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
        return TestClient(oauth_gateway._build_app(transport='streamable-http'))

    def test_authorization_server_metadata(self, oauth_client):
        """OAuth metadata document advertises issuer, endpoints and PKCE method."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        data = response.json()
        assert 'petstore' in data['issuer']
        assert data['authorization_endpoint'].endswith('/authorize')
        assert data['token_endpoint'].endswith('/token')
        assert 'S256' in data['code_challenge_methods_supported']

    @pytest.mark.parametrize(
        'path',
        ['/.well-known/oauth-authorization-server/petstore', '/petstore/.well-known/oauth-authorization-server'],
    )
    def test_metadata_advertises_the_iss_parameter(self, oauth_client, path):
        """Both metadata locations promise ``iss`` on authorization responses, which the provider sends (RFC 9207)."""
        response = oauth_client.get(path)
        assert response.status_code == 200
        assert response.json()['authorization_response_iss_parameter_supported'] is True

    def test_metadata_documents_are_announced_while_unlisted_clients_may_sign_in(self, oauth_client):
        """The MCP spec has a client prefer its metadata document over registering where this is announced."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.json()['client_id_metadata_document_supported'] is True

    def test_a_closed_set_of_plain_clients_announces_no_metadata_documents(self, petstore_json_path):
        gateway = authorization_code_gateway(petstore_json_path, {'agent': {}})
        response = TestClient(gateway._build_app(transport='streamable-http')).get(
            '/.well-known/oauth-authorization-server/petstore'
        )
        assert 'client_id_metadata_document_supported' not in response.json()

    def test_authorization_server_with_mcp(self, oauth_client):
        """Suffixing ``/mcp`` on the metadata path is also served."""
        response = oauth_client.get('/.well-known/oauth-authorization-server/petstore/mcp')
        assert response.status_code == 200

    def test_protected_resource_metadata(self, oauth_client):
        """Protected-resource metadata points to the MCP endpoint and authorization servers."""
        response = oauth_client.get('/.well-known/oauth-protected-resource/petstore')
        assert response.status_code == 200
        data = response.json()
        assert data['resource'].endswith('/mcp')
        assert len(data['authorization_servers']) == 1

    def test_options_cors(self, oauth_client):
        """``OPTIONS`` on the metadata endpoint returns 200 for CORS preflight."""
        response = oauth_client.options('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200


class TestWellKnownUnderPathPrefix:
    """Discovery details an MCP SDK client cannot show, which ``test_mcp_client_sign_in`` covers end to end."""

    def test_the_published_urls_are_normalized(self, petstore_json_path):
        """An upper-case host and a default port in ``url`` are published as a client would echo them."""
        http = TestClient(_shared_credential_gateway(petstore_json_path, 'https://MCP.Example.com:443', 'pets'))

        resource = http.get('/.well-known/oauth-protected-resource/pets/mcp').json()
        assert resource['resource'] == f'{GATEWAY_URL}/pets/mcp'
        assert resource['authorization_servers'] == [f'{GATEWAY_URL}/pets']
        token = http.post(
            '/pets/token',
            data={'grant_type': 'client_credentials', 'resource': f'{GATEWAY_URL}/pets/mcp'},
            auth=('agent', 'agent-secret'),
        )
        assert token.status_code == 200, token.text

    def test_the_server_name_still_finds_the_documents(self, petstore_json_path):
        """The URLs served before discovery followed the mount path keep working."""
        http = TestClient(_shared_credential_gateway(petstore_json_path, GATEWAY_URL, 'pets'))

        assert http.get('/.well-known/oauth-authorization-server/petstore').status_code == 200
        assert http.get('/.well-known/oauth-protected-resource/petstore/mcp').status_code == 200


class TestMountEmbedding:
    """``Gateway.mount`` wires OAuth and ``.well-known`` routes onto a host FastAPI app."""

    def test_mount_registers_well_known_routes(self, oauth_gateway):
        """``mount`` makes the host app serve the OAuth discovery metadata."""
        host = FastAPI()
        oauth_gateway.mount(host, transport='streamable-http')
        client = TestClient(host)

        response = client.get('/.well-known/oauth-authorization-server/petstore')
        assert response.status_code == 200
        assert response.json()['authorization_endpoint'].endswith('/authorize')

    async def test_mounted_gateway_answers_mcp_inside_the_host_lifespan(self, gateway):
        """A host app that enters ``gateway.lifespan`` serves MCP through the mounted gateway."""

        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI) -> typing.AsyncIterator[None]:
            async with gateway.lifespan():
                yield

        host = FastAPI(lifespan=lifespan)
        gateway.mount(host, transport='streamable-http')

        with anyio.fail_after(10):
            async with serve(host) as base_url, Client(f'{base_url}/petstore/mcp') as mcp_client:
                tools = await mcp_client.list_tools()

        assert tools.tools


def _shared_credential_gateway(spec_path: pathlib.Path, gateway_url: str, path_prefix: str) -> FastAPI:
    """The app of a gateway at ``gateway_url`` issuing MCP tokens for one bearer-token server under ``path_prefix``."""
    server = shared_credential_server(spec_path, 'petstore', path_prefix)
    gateway = Gateway.from_config(GatewayConfig.model_validate({'url': gateway_url, 'servers': [server]}))
    return gateway._build_app(transport='streamable-http')
