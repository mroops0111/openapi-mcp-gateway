from starlette.types import ASGIApp, Receive, Scope, Send

from tests.gateway import build_gateway
from tests.mcp_client import agent_auth, list_tool_names
from tests.oauth import shared_credential_server
from tests.serving import serve_gateway


class TestBehindAProxy:
    """A gateway behind a proxy that strips its path signs clients in at the public url."""

    async def test_a_client_signs_in_behind_a_proxy_that_strips_the_gateway_path(self, shop_json_path):
        servers = [shared_credential_server(shop_json_path, 'shop', 'products')]
        async with serve_gateway(
            lambda url: build_gateway(f'{url}/gw', servers),
            lambda gateway: _strip_path_prefix(gateway.build_app(), '/gw'),
        ) as base_url:
            endpoint = f'{base_url}/gw/products/mcp'
            assert await list_tool_names(endpoint, agent_auth(endpoint))


def _strip_path_prefix(app: ASGIApp, prefix: str) -> ASGIApp:
    """``app`` behind a proxy that strips ``prefix`` from each request path before forwarding it."""

    async def proxy(scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            path = scope['path'].removeprefix(prefix) or '/'
            scope = {**scope, 'path': path, 'raw_path': path.encode()}
        await app(scope, receive, send)

    return proxy
