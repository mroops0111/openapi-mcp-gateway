import contextlib
import dataclasses
import typing
from unittest.mock import AsyncMock, patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthToken
from mcp.types import CallToolResult

from openapi_mcp_gateway.auth.mcp.authorization_code import McpAuthorizationCodeServer
from openapi_mcp_gateway.gateway import Gateway
from tests.constants import GATEWAY_URL
from tests.mcp_client import Browser, MemoryTokenStorage, authorization_code_auth, connect
from tests.oauth import server_of, sign_in, upstream_token_response
from tests.serving import serve, serve_gateway


# The protocol revisions a client may speak.
# Only 2026-07-28 carries the routing headers an HTTP-layer scope challenge is read from.
MODERN = '2026-07-28'
LEGACY = 'legacy'

# The pre-registered clients every scoped server declares, by what each may do to products.
# ``stepper`` starts with ``products:write`` and may step up for the rest.
READER = 'reader'
DELETER = 'deleter'
WRITER = 'writer'
STEPPER = 'stepper'
CLIENTS = {
    READER: {'scopes': ['products:read']},
    DELETER: {'scopes': ['products:read', 'products:delete']},
    WRITER: {'scopes': ['products:write']},
    STEPPER: {'scopes': ['products:read', 'products:write', 'products:delete'], 'default_scopes': ['products:write']},
}

# The tools a token holding only ``products:read`` sees on the shop fixture.
READ_TOOLS = ['list_products', 'get_product_by_id']

# The challenges a token is answered with when its scopes lack the one named.
DELETE_CHALLENGE = (
    'Bearer error="insufficient_scope", scope="products:delete", '
    f'resource_metadata="{GATEWAY_URL}/.well-known/oauth-protected-resource/shop/mcp", '
    'error_description="This operation needs one of these scopes: products:delete."'
)
READ_CHALLENGE = DELETE_CHALLENGE.replace('products:delete', 'products:read')

# A product read through the resource the shop's ``getProductById`` is promoted to.
PRODUCT_RESOURCE = 'shop://products/7'


@dataclasses.dataclass
class Session:
    """An MCP client connected with one token, and every HTTP response it received, in order."""

    client: Client
    responses: list[httpx2.Response]

    @property
    def refusal(self) -> httpx2.Response:
        """The last response, which refused the call that just failed."""
        return self.responses[-1]


async def sign_in_as(gateway: Gateway, client_id: str, requested: str | None = None, upstream_scope: str | None = None):
    """Sign ``client_id`` in through the gateway's own authorization server, asking for ``requested``.

    The upstream states ``upstream_scope`` as the scope it granted, or omits it when ``None``.
    """
    provider = provider_of(gateway)
    client = await provider.get_client(client_id)
    assert client is not None
    return await sign_in(provider, client, scopes=client.validate_scope(requested), upstream_scope=upstream_scope)


def provider_of(gateway: Gateway) -> McpAuthorizationCodeServer:
    """The authorization server of the gateway's one server."""
    return server_of(gateway.describe_servers()[0].auth.provider)


@contextlib.asynccontextmanager
async def connect_as(
    gateway: Gateway, client_id: str, mode: str, upstream_scope: str | None = None
) -> typing.AsyncIterator[Session]:
    """Serve ``gateway`` over HTTP and connect an MCP client speaking ``mode``, signed in as ``client_id``.

    The upstream granted ``upstream_scope``, or what the gateway requested when ``None``.
    """
    token = await sign_in_as(gateway, client_id, upstream_scope=upstream_scope)
    responses: list[httpx2.Response] = []

    async def record(response: httpx2.Response) -> None:
        responses.append(response)

    http_client = httpx2.AsyncClient(
        headers={'Authorization': f'Bearer {token.access_token}'}, event_hooks={'response': [record]}
    )
    async with serve(gateway.build_app()) as base_url:
        transport = streamable_http_client(f'{base_url}/shop/mcp', http_client=http_client)
        async with Client(transport, mode=mode) as client:
            yield Session(client, responses)


async def listed_tool_names(session: Session) -> list[str]:
    """The tools ``tools/list`` shows the session's token, in registration order."""
    return [tool.name for tool in (await session.client.list_tools()).tools]


async def sign_in_with_the_sdk(build: typing.Callable[[str], Gateway], client_id: str) -> Browser:
    """Sign ``client_id`` in with the SDK's own client to the gateway ``build`` makes, and return the browser used."""
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(build) as base_url:
            endpoint = f'{base_url}/shop/mcp'
            browser = Browser(f'{base_url}/shop')
            async with connect(endpoint, authorization_code_auth(endpoint, browser, client_id)):
                pass
    return browser


async def step_up_with_the_sdk(
    build: typing.Callable[[str], Gateway], client_id: str, tool: str, arguments: dict[str, typing.Any]
) -> tuple[Browser, CallToolResult]:
    """Sign ``client_id`` in with the SDK's own client and call ``tool``, which its default scopes cannot use.

    The upstream grants whatever each sign-in asks for, so only the MCP scopes stand in the way of the first call.
    """
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(build) as base_url:
            endpoint = f'{base_url}/shop/mcp'
            browser = Browser(f'{base_url}/shop')
            async with connect(endpoint, authorization_code_auth(endpoint, browser, client_id)) as client:
                result = await client.call_tool(tool, arguments)
    return browser, result


async def sign_in_through_the_sdk(make_gateway: typing.Callable[..., Gateway], client_id: str) -> OAuthToken:
    """Sign ``client_id`` in with the SDK's own client, through consent and the upstream, and return its tokens."""
    storage = MemoryTokenStorage()
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        async with serve_gateway(lambda url: make_gateway(url=url)) as base_url:
            endpoint = f'{base_url}/shop/mcp'
            auth = authorization_code_auth(endpoint, Browser(f'{base_url}/shop'), client_id, storage=storage)
            async with connect(endpoint, auth):
                pass
    assert storage.tokens is not None
    return storage.tokens
