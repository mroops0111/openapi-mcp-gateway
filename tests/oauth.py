import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from openapi_mcp_gateway.auth.flows.authorization_code import AuthorizationCodeProvider


# Where the MCP client asks to be sent back once it is authorized, a loopback address as a CLI client uses.
CLIENT_REDIRECT_URI = 'http://localhost:3000/callback'


def upstream_token_response(granted: str = 'upstream-access-token') -> MagicMock:
    """Mock upstream token endpoint response granting ``granted`` plus a refresh token."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'access_token': granted,
        'refresh_token': 'upstream-refresh-token',
        'expires_in': 3600,
    }
    return response


async def authorize(
    provider: AuthorizationCodeProvider,
    client: OAuthClientInformationFull,
    state: str,
    scopes: list[str] | None = None,
) -> str:
    """Start a sign-in for ``client`` under ``state``, returning the upstream authorize URL.

    ``scopes`` is what the SDK's authorize handler settled on, which is ``None`` when the client asked for none.
    """
    params = AuthorizationParams(
        state=state,
        scopes=scopes,
        redirect_uri=AnyHttpUrl(CLIENT_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        code_challenge='challenge-abc',
    )
    return await provider.authorize(client, params)


async def complete_upstream(provider: AuthorizationCodeProvider, state: str, iss: str | None = None) -> str:
    """Deliver the upstream's redirect for ``state``, returning the redirect to the MCP client."""
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
        return await provider.handle_upstream_callback('upstream-code', state, iss)


def code_in(redirect: str) -> str:
    """The MCP authorization code carried by a redirect to the MCP client."""
    return urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)['code'][0]


async def sign_in(
    provider: AuthorizationCodeProvider,
    client: OAuthClientInformationFull,
    state: str = 'state-sign-in',
    scopes: list[str] | None = None,
) -> OAuthToken:
    """Run a whole authorization through ``provider`` and return the MCP tokens it issues."""
    await authorize(provider, client, state, scopes)
    auth_code = await provider.load_authorization_code(client, code_in(await complete_upstream(provider, state)))
    assert auth_code is not None
    return await provider.exchange_authorization_code(client, auth_code)
