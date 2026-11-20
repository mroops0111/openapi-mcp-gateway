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
from starlette.types import ASGIApp, Receive, Scope, Send

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.mcp_client import Browser, authorization_code_auth, client_credentials_auth, connect
from tests.oauth import shared_credential_server, upstream_token_response
from tests.serving import bind_loopback_socket, build_socket_url, serve_gateway


class TestClientCredentials:
    """An MCP SDK client finds the token endpoint from the endpoint alone, signs in, and lists the tools."""

    @pytest.mark.parametrize('path_prefix', ['pets', 'apis/pets', '/'])
    async def test_a_client_signs_in_wherever_the_server_is_mounted(self, petstore_json_path, path_prefix):
        servers = [shared_credential_server(petstore_json_path, 'petstore', path_prefix)]
        async with serve_gateway(lambda url: _build_gateway(url, servers)) as base_url:
            mount = path_prefix.strip('/')
            endpoint = f'{base_url}/{mount}/mcp' if mount else f'{base_url}/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_a_client_signs_in_behind_a_proxy_that_strips_the_gateway_path(self, petstore_json_path):
        servers = [shared_credential_server(petstore_json_path, 'petstore', 'pets')]
        async with serve_gateway(
            lambda url: _build_gateway(f'{url}/gw', servers),
            lambda gateway: _strip_path_prefix(gateway._build_app(transport='streamable-http'), '/gw'),
        ) as base_url:
            endpoint = f'{base_url}/gw/pets/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_each_server_signs_in_when_one_is_mounted_at_the_others_name(self, petstore_json_path):
        servers = [
            shared_credential_server(petstore_json_path, 'alpha', 'beta'),
            shared_credential_server(petstore_json_path, 'beta2', 'alpha'),
        ]
        async with serve_gateway(lambda url: _build_gateway(url, servers)) as base_url:
            for path in ('alpha', 'beta'):
                endpoint = f'{base_url}/{path}/mcp'
                assert await _list_tools(endpoint, _agent_auth(endpoint))

    async def test_a_client_signs_in_to_a_gateway_mounted_into_a_host_app(self, petstore_json_path):
        servers = [shared_credential_server(petstore_json_path, 'petstore', 'pets')]
        async with serve_gateway(lambda url: _build_gateway(url, servers), _mount_into_a_host_app) as base_url:
            endpoint = f'{base_url}/pets/mcp'
            assert await _list_tools(endpoint, _agent_auth(endpoint))
            async with httpx2.AsyncClient() as http:
                host_document = await http.get(f'{base_url}/.well-known/oauth-authorization-server')
            assert host_document.json() == {'issuer': 'host'}


class TestAuthorizationCode:
    """An MCP SDK client registers, sends the user through consent and the upstream, and lists the tools."""

    @pytest.mark.parametrize('path_prefix', ['petstore', '/'])
    async def test_a_client_signs_a_user_in(self, petstore_json_path, path_prefix):
        server = {
            'name': 'petstore',
            'path_prefix': path_prefix,
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
            },
        }
        mount = '' if path_prefix == '/' else f'/{path_prefix}'

        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            async with serve_gateway(lambda url: _build_gateway(url, [server])) as base_url:
                endpoint = f'{base_url}{mount}/mcp'
                auth = authorization_code_auth(endpoint, Browser(f'{base_url}{mount}'))
                assert await _list_tools(endpoint, auth)


class TestTokenExchange:
    """An MCP SDK client finds the external issuer from the endpoint, gets a token there, and lists the tools."""

    async def test_a_client_signs_in_at_the_external_issuer(self, petstore_json_path):
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer_sock = bind_loopback_socket()
        issuer_url = build_socket_url(issuer_sock)
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

        with _serve_in_thread(_build_external_issuer(issuer_url, signing_key), issuer_sock):
            async with serve_gateway(lambda url: _build_gateway(url, [server])) as base_url:
                endpoint = f'{base_url}/petstore/mcp'
                auth = client_credentials_auth(endpoint, 'agent', 'agent-secret', issuer=issuer_url)
                assert await _list_tools(endpoint, auth)


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


def _mount_into_a_host_app(gateway: Gateway) -> FastAPI:
    """A host app that mounts ``gateway``, then serves a discovery document of its own the gateway does not claim."""

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

    return host
