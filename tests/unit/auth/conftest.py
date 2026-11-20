import typing

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from openapi_mcp_gateway.auth.mcp.authorization_code import McpAuthorizationCodeServer
from openapi_mcp_gateway.auth.mcp.token_issuer import McpTokenPolicy
from openapi_mcp_gateway.auth.upstream.authorization_code import UpstreamAuthorizationCodeClient
from tests.constants import (
    AUTHORIZE_URL,
    GATEWAY_ISSUER,
    TOKEN_URL,
)
from tests.oauth import (
    CLIENT_REDIRECT_URI,
    authorization_code_server,
)


@pytest.fixture
def make_provider(memory_store) -> typing.Callable[..., McpAuthorizationCodeServer]:
    """Build a provider on the shared store, naming ``issuer`` as its own in front of the upstream."""

    def make(
        issuer: str = GATEWAY_ISSUER,
        upstream_issuer: str | None = None,
        token_url: str = TOKEN_URL,
        scopes: list[str] | None = None,
        audience_params: dict[str, str] | None = None,
        issued_tokens: McpTokenPolicy | None = None,
    ) -> McpAuthorizationCodeServer:
        return authorization_code_server(
            memory_store,
            issuer,
            UpstreamAuthorizationCodeClient(
                authorization_url=AUTHORIZE_URL,
                token_url=token_url,
                client_id='gateway-client-id',
                client_secret='gateway-client-secret',
                callback_url=f'{issuer}/auth/callback',
                scopes=scopes or ['read'],
                audience_params=audience_params or {},
                issuer=upstream_issuer,
            ),
            issued_tokens,
        )

    return make


@pytest.fixture
def provider(make_provider):
    """The petstore server's provider, requesting ``read`` and ``write`` upstream."""
    return make_provider(scopes=['read', 'write'])


@pytest.fixture
def mcp_client_info():
    """An MCP client as the SDK's registration handler would record it."""
    return OAuthClientInformationFull(
        client_id='mcp-client-123',
        client_secret='mcp-secret',
        redirect_uris=[AnyHttpUrl(CLIENT_REDIRECT_URI)],
    )
