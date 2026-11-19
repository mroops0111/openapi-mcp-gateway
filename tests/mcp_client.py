import contextlib
import typing
import urllib.parse

import httpx2
from mcp import Client
from mcp.client.auth import TokenStorage
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


@contextlib.asynccontextmanager
async def connect(endpoint: str, auth: httpx2.Auth, **options: typing.Any) -> typing.AsyncIterator[Client]:
    """An MCP SDK client of ``endpoint`` signed in through ``auth``, built with ``options`` such as ``mode``."""
    async with (
        httpx2.AsyncClient(auth=auth) as http_client,
        Client(streamable_http_client(endpoint, http_client=http_client), **options) as client,
    ):
        yield client


def client_credentials_auth(
    endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str | None = None,
    storage: TokenStorage | None = None,
) -> ClientCredentialsOAuthProvider:
    """The SDK's own client_credentials sign-in at the gateway serving ``endpoint``, whose issuer it names."""
    issuer = endpoint.removesuffix('/mcp')
    return ClientCredentialsOAuthProvider(
        server_url=endpoint,
        storage=storage or MemoryTokenStorage(),
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        # The gateway publishes a root issuer with its trailing slash, as ``pydantic.AnyHttpUrl`` renders it.
        issuer=issuer if urllib.parse.urlparse(issuer).path else f'{issuer}/',
    )


class MemoryTokenStorage(TokenStorage):
    """Where the SDK client keeps the token it obtained, read back by the test."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info
