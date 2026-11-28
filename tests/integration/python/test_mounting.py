import contextlib
import typing

import anyio
import httpx2
from fastapi import FastAPI
from mcp import Client
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from tests.gateway import build_gateway
from tests.mcp_client import (
    agent_auth,
    list_tool_names,
)
from tests.oauth import shared_credential_server
from tests.serving import serve, serve_gateway


class TestMountEmbedding:
    """``Gateway.mount`` wires OAuth and ``.well-known`` routes onto a host FastAPI app."""

    def test_mount_registers_well_known_routes(self, oauth_gateway):
        """``mount`` makes the host app serve the OAuth discovery metadata."""
        host = FastAPI()
        oauth_gateway.mount(host, transport='streamable-http')
        client = TestClient(host)

        response = client.get('/.well-known/oauth-authorization-server/shop')
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
            async with serve(host) as base_url, Client(f'{base_url}/shop/mcp') as mcp_client:
                tools = await mcp_client.list_tools()

        assert tools.tools


class TestSdkSignIn:
    """An MCP SDK client signs in to a gateway mounted into a host app, which keeps its own routes."""

    async def test_a_client_signs_in_to_a_gateway_mounted_into_a_host_app(self, shop_json_path):
        servers = [shared_credential_server(shop_json_path, 'shop', 'products')]
        async with serve_gateway(lambda url: build_gateway(url, servers), _mount_into_a_host_app) as base_url:
            endpoint = f'{base_url}/products/mcp'
            assert await list_tool_names(endpoint, agent_auth(endpoint))
            async with httpx2.AsyncClient() as http:
                host_document = await http.get(f'{base_url}/.well-known/oauth-authorization-server')
            assert host_document.json() == {'issuer': 'host'}


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
