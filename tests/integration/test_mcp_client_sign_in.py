import contextlib
import typing
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from openapi_mcp_gateway.auth.mcp.client_metadata import McpClientMetadataResolver
from openapi_mcp_gateway.auth.mcp.token_issuer import MCP_DEFAULT_SCOPE
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.client_metadata import (
    CLIENT_METADATA_HOST,
    CLIENT_METADATA_URL,
    client_metadata_document,
    serving_resolver,
)
from tests.fake_idp import FakeIdentityProvider, serve_identity_provider
from tests.mcp_client import Browser, MemoryTokenStorage, authorization_code_auth, client_credentials_auth, connect
from tests.oauth import shared_credential_server, upstream_token_response
from tests.serving import serve_gateway


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


class TestClientIdMetadataDocument:
    """An MCP SDK client signs in with its metadata document's URL as its client_id, where the gateway accepts one.

    The document lists its loopback redirect without a port, and the client listens on one of its choosing.
    """

    @pytest.mark.parametrize(
        ('mcp', 'scope'),
        [
            ({}, MCP_DEFAULT_SCOPE),
            (
                {
                    'operation_scopes': {'auto': 'tags'},
                    'other_clients': {'trusted_hosts': [CLIENT_METADATA_HOST], 'scopes': ['pets:read']},
                },
                'pets:read',
            ),
            (
                {'operation_scopes': {'auto': 'tags'}, 'clients': {CLIENT_METADATA_URL: {'scopes': ['pets:write']}}},
                'pets:write',
            ),
        ],
        ids=['open by default', 'other clients', 'listed'],
    )
    async def test_a_client_signs_a_user_in_without_registering(self, petstore_json_path, mcp, scope):
        server = {
            'name': 'petstore',
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
                'mcp': mcp,
            },
        }
        storage = MemoryTokenStorage()
        resolver = serving_resolver({CLIENT_METADATA_URL: client_metadata_document()})
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            async with serve_gateway(lambda url: _build_gateway(url, [server], resolver)) as base_url:
                endpoint = f'{base_url}/petstore/mcp'
                auth = authorization_code_auth(
                    endpoint, Browser(f'{base_url}/petstore'), storage=storage, client_metadata_url=CLIENT_METADATA_URL
                )
                assert await _list_tools(endpoint, auth)
        assert storage.client_info is not None
        assert storage.client_info.client_id == CLIENT_METADATA_URL
        assert storage.tokens is not None
        assert storage.tokens.scope == scope


class TestTokenExchange:
    """An MCP SDK client finds the external issuer from the endpoint, gets a token there, and lists the tools."""

    async def test_a_client_signs_in_at_the_external_issuer(self, petstore_json_path, signing_key):
        identity_provider = FakeIdentityProvider(signing_key, {'agent': 'agent-secret'}, {'agent': {'pets:read'}})
        with serve_identity_provider(identity_provider) as issuer:
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
                    'mcp': {'issuer': issuer, 'required_scopes': ['pets:read']},
                },
            }
            async with serve_gateway(lambda url: _build_gateway(url, [server])) as base_url:
                endpoint = f'{base_url}/petstore/mcp'
                auth = client_credentials_auth(endpoint, 'agent', 'agent-secret', issuer=issuer)
                assert await _list_tools(endpoint, auth)


def _build_gateway(
    gateway_url: str,
    servers: list[dict[str, typing.Any]],
    client_metadata_resolver: McpClientMetadataResolver | None = None,
) -> Gateway:
    return Gateway.from_config(
        GatewayConfig.model_validate({'url': gateway_url, 'servers': servers}),
        client_metadata_resolver=client_metadata_resolver,
    )


def _strip_path_prefix(app: ASGIApp, prefix: str) -> ASGIApp:
    """``app`` behind a proxy that strips ``prefix`` from each request path before forwarding it."""

    async def proxy(scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            path = scope['path'].removeprefix(prefix) or '/'
            scope = {**scope, 'path': path, 'raw_path': path.encode()}
        await app(scope, receive, send)

    return proxy


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
