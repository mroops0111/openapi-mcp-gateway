import contextlib
import socket
import threading
import time
import typing
from unittest.mock import AsyncMock, patch

import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from starlette.types import ASGIApp, Receive, Scope, Send

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.mcp_client import MemoryTokenStorage, client_credentials_auth, connect
from tests.oauth import query_value, shared_credential_server, upstream_token_response
from tests.serving import bind_loopback_socket, build_socket_url, serve


# Where the MCP client sends the browser back to, which it never serves since the test plays the browser.
CLIENT_CALLBACK = 'http://127.0.0.1:3030/callback'


class TestClientCredentials:
    """An MCP SDK client finds the token endpoint from the endpoint alone, signs in, and lists the tools."""

    @pytest.mark.parametrize('path_prefix', ['pets', 'apis/pets', '/'])
    async def test_a_client_signs_in_wherever_the_server_is_mounted(self, petstore_json_path, path_prefix):
        sock = bind_loopback_socket()
        base_url = build_socket_url(sock)
        gateway = _build_gateway(base_url, [shared_credential_server(petstore_json_path, 'petstore', path_prefix)])

        async with serve(gateway._build_app(transport='streamable-http'), sock):
            mount = path_prefix.strip('/')
            endpoint = f'{base_url}/{mount}/mcp' if mount else f'{base_url}/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_a_client_signs_in_behind_a_proxy_that_strips_the_gateway_path(self, petstore_json_path):
        sock = bind_loopback_socket()
        gateway_url = f'{build_socket_url(sock)}/gw'
        gateway = _build_gateway(gateway_url, [shared_credential_server(petstore_json_path, 'petstore', 'pets')])

        async with serve(_strip_path_prefix(gateway._build_app(transport='streamable-http'), '/gw'), sock):
            endpoint = f'{gateway_url}/pets/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_each_server_signs_in_when_one_is_mounted_at_the_others_name(self, petstore_json_path):
        sock = bind_loopback_socket()
        base_url = build_socket_url(sock)
        servers = [
            shared_credential_server(petstore_json_path, 'alpha', 'beta'),
            shared_credential_server(petstore_json_path, 'beta2', 'alpha'),
        ]
        gateway = _build_gateway(base_url, servers)

        async with serve(gateway._build_app(transport='streamable-http'), sock):
            for path in ('alpha', 'beta'):
                endpoint = f'{base_url}/{path}/mcp'
                assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_a_client_signs_in_to_a_gateway_mounted_into_a_host_app(self, petstore_json_path):
        sock = bind_loopback_socket()
        base_url = build_socket_url(sock)
        gateway = _build_gateway(base_url, [shared_credential_server(petstore_json_path, 'petstore', 'pets')])

        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI) -> typing.AsyncIterator[None]:
            async with gateway.lifespan():
                yield

        host = FastAPI(lifespan=lifespan)
        gateway.mount(host, transport='streamable-http')

        # Registered after the gateway's routes, so only a path the gateway does not claim reaches it.
        @host.get('/.well-known/oauth-authorization-server')
        async def host_metadata():
            return {'issuer': 'host'}

        async with serve(host, sock):
            endpoint = f'{base_url}/pets/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))
            async with httpx2.AsyncClient() as http:
                host_document = await http.get(f'{base_url}/.well-known/oauth-authorization-server')
            assert host_document.json() == {'issuer': 'host'}


class TestAuthorizationCode:
    """An MCP SDK client registers, sends the user through consent and the upstream, and lists the tools."""

    @pytest.mark.parametrize('path_prefix', ['petstore', '/'])
    async def test_a_client_signs_a_user_in(self, petstore_json_path, path_prefix):
        sock = bind_loopback_socket()
        base_url = build_socket_url(sock)
        server = {
            'name': 'petstore',
            'path_prefix': path_prefix,
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
            },
        }
        gateway = _build_gateway(base_url, [server])
        mount = '' if path_prefix == '/' else f'/{path_prefix}'
        browser = _Browser(f'{base_url}{mount}')
        endpoint = f'{base_url}{mount}/mcp'
        auth = OAuthClientProvider(
            server_url=endpoint,
            client_metadata=OAuthClientMetadata(redirect_uris=[CLIENT_CALLBACK], client_name='agent'),  # type: ignore[list-item]
            storage=MemoryTokenStorage(),
            redirect_handler=browser.sign_in,
            callback_handler=browser.return_to_client,
        )

        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            async with serve(gateway._build_app(transport='streamable-http'), sock):
                assert await _list_tools(endpoint, auth)


class TestTokenExchange:
    """An MCP SDK client finds the external issuer from the endpoint, gets a token there, and lists the tools."""

    async def test_a_client_signs_in_at_the_external_issuer(self, petstore_json_path):
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer_sock = bind_loopback_socket()
        issuer_url = build_socket_url(issuer_sock)
        sock = bind_loopback_socket()
        base_url = build_socket_url(sock)
        server = {
            'name': 'petstore',
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {
                    'type': 'oauth2',
                    'flow': 'token_exchange',
                    'audience': 'https://api.example.com',
                    'client_id': 'gateway',
                    'client_secret': 'secret',
                    'scopes': ['read'],
                },
                'mcp': {'issuer': issuer_url},
            },
        }
        endpoint = f'{base_url}/petstore/mcp'

        with _serve_in_thread(_build_external_issuer(issuer_url, signing_key), issuer_sock):
            gateway = _build_gateway(base_url, [server])
            async with serve(gateway._build_app(transport='streamable-http'), sock):
                auth = ClientCredentialsOAuthProvider(
                    server_url=endpoint,
                    storage=MemoryTokenStorage(),
                    client_id='agent',
                    client_secret='agent-secret',
                    issuer=issuer_url,
                )
                assert await _list_tools(endpoint, auth)


class _Browser:
    """The user's browser, which approves the client and plays the upstream sending the user back."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url
        self._client_redirect: str | None = None

    async def sign_in(self, authorization_url: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=False) as browser:
            consent_url = (await browser.get(authorization_url)).headers['location']
            page = await browser.get(consent_url)
            csrf_token = page.text.split('name="csrf_token" value="')[1].split('"')[0]
            upstream_url = (
                await browser.post(consent_url, data={'csrf_token': csrf_token, 'decision': 'approve'})
            ).headers['location']
            callback = await browser.get(
                f'{self.server_url}/auth/callback',
                params={'code': 'upstream-code', 'state': query_value(upstream_url, 'state')},
            )
            self._client_redirect = callback.headers['location']

    async def return_to_client(self) -> AuthorizationCodeResult:
        """What the redirect to the client carries, ``iss`` included, which the SDK checks per RFC 9207."""
        assert self._client_redirect is not None
        return AuthorizationCodeResult(
            code=query_value(self._client_redirect, 'code'),
            state=query_value(self._client_redirect, 'state'),
            iss=query_value(self._client_redirect, 'iss'),
        )


def _build_gateway(gateway_url: str, servers: list[dict[str, typing.Any]]) -> Gateway:
    return Gateway.from_config(GatewayConfig.model_validate({'url': gateway_url, 'servers': servers}))


def _strip_path_prefix(app: ASGIApp, prefix: str) -> ASGIApp:
    """``app`` behind a proxy that strips ``prefix`` from each request path before forwarding it."""

    async def proxy(scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            path = scope['path'].removeprefix(prefix) or '/'
            scope = {**scope, 'path': path, 'raw_path': path.encode()}
        await app(scope, receive, send)

    return proxy


@contextlib.contextmanager
def _serve_in_thread(app: FastAPI, sock: socket.socket) -> typing.Iterator[None]:
    """Serve ``app`` on its own thread, since the gateway reads an external issuer's documents synchronously."""
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, ws='none'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline, 'the external issuer failed to start'
        time.sleep(0.01)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(5)


def _build_external_issuer(issuer_url: str, signing_key: rsa.RSAPrivateKey) -> FastAPI:
    """An authorization server outside the gateway, issuing JWTs for the resource each client names."""
    app = FastAPI()
    jwk = RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)

    @app.get('/.well-known/oauth-authorization-server')
    async def metadata():
        return {
            'issuer': issuer_url,
            'authorization_endpoint': f'{issuer_url}/authorize',
            'token_endpoint': f'{issuer_url}/token',
            'jwks_uri': f'{issuer_url}/jwks',
            'response_types_supported': ['code'],
            'grant_types_supported': ['client_credentials', 'urn:ietf:params:oauth:grant-type:token-exchange'],
        }

    @app.get('/jwks')
    async def jwks():
        return {'keys': [{**jwk, 'kid': 'key-1', 'use': 'sig', 'alg': 'RS256'}]}

    @app.post('/token')
    async def token(resource: typing.Annotated[str, Form()]):
        claims = {'iss': issuer_url, 'aud': resource, 'sub': 'agent', 'exp': int(time.time()) + 300}
        access_token = jwt.encode(claims, signing_key, algorithm='RS256', headers={'kid': 'key-1'})
        return JSONResponse({'access_token': access_token, 'token_type': 'Bearer', 'expires_in': 300})

    return app


async def _list_tools(endpoint: str, auth: httpx2.Auth) -> list[str]:
    """The tools an MCP client signed in through ``auth`` sees at ``endpoint``."""
    async with connect(endpoint, auth) as client:
        return [tool.name for tool in (await client.list_tools()).tools]


def _agent_auth(endpoint: str) -> httpx2.Auth:
    """The client_credentials sign-in of the agent ``shared_credential_server`` declares."""
    return client_credentials_auth(endpoint, 'agent', 'agent-secret', 'pets:read')
