import pathlib
import typing
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from openapi_mcp_gateway.auth.flows.authorization_code import AuthorizationCodeProvider
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import GATEWAY_URL


# Where the MCP client asks to be sent back once it is authorized, a loopback address as a CLI client uses.
CLIENT_REDIRECT_URI = 'http://localhost:3000/callback'

# The ``state`` the MCP client sends, which it expects back unchanged.
CLIENT_STATE = 'client-state'

# The browser a sign-in runs in, as its cookie names it.
BROWSER_ID = 'b0' * 32

# The browser cookie's name over HTTPS, and over plain HTTP, where the ``__Host-`` prefix cannot be set.
SECURE_COOKIE = '__Host-openapi-mcp-gateway-browser'
PLAIN_COOKIE = 'openapi-mcp-gateway-browser'


def authorization_code_gateway(
    spec: pathlib.Path,
    clients: dict[str, typing.Any],
    operation_scopes: dict[str, typing.Any] | None = None,
    upstream: dict[str, typing.Any] | None = None,
    exposure: dict[str, typing.Any] | None = None,
) -> Gateway:
    """A gateway serving ``spec`` as ``petstore`` behind an ``authorization_code`` server declaring ``clients``.

    ``upstream`` is merged into ``auth.upstream``, which otherwise requests ``read``.
    """
    auth = {
        'type': 'oauth2',
        'upstream': {'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read'], **(upstream or {})},
        'clients': clients,
        **({'operation_scopes': operation_scopes} if operation_scopes is not None else {}),
    }
    server = {'name': 'petstore', 'spec': str(spec), 'auth': auth, 'exposure': exposure or {}}
    return Gateway.from_config(GatewayConfig.model_validate({'url': GATEWAY_URL, 'servers': [server]}))


def upstream_token_response(granted: str = 'upstream-access-token', scope: str | None = None) -> MagicMock:
    """Mock upstream token endpoint response granting ``granted`` plus a refresh token.

    ``scope`` is the granted scope it states, which it omits when ``None``.
    """
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'access_token': granted,
        'refresh_token': 'upstream-refresh-token',
        'expires_in': 3600,
        **({'scope': scope} if scope is not None else {}),
    }
    return response


async def authorize(
    provider: AuthorizationCodeProvider,
    client: OAuthClientInformationFull,
    state: str | None = CLIENT_STATE,
    scopes: list[str] | None = None,
) -> str:
    """Start a sign-in for ``client`` under its own ``state``, returning the consent page URL the browser is sent to.

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


async def begin_upstream(
    provider: AuthorizationCodeProvider,
    client: OAuthClientInformationFull,
    state: str | None = CLIENT_STATE,
    scopes: list[str] | None = None,
) -> str:
    """Start a sign-in and take it past the consent page in ``BROWSER_ID``, returning the upstream authorize URL."""
    pending = await provider.claim_authorization(
        request_id_in(await authorize(provider, client, state, scopes)), BROWSER_ID
    )
    assert pending is not None
    return await provider.continue_to_upstream(pending)


async def complete_upstream(
    provider: AuthorizationCodeProvider,
    upstream_url: str,
    iss: str | None = None,
    upstream_scope: str | None = None,
) -> str:
    """Deliver the upstream's redirect for the sign-in ``upstream_url`` began, returning the redirect to the MCP client.

    The upstream states ``upstream_scope`` as the scope it granted, or omits it when ``None``.
    """
    response = upstream_token_response(scope=upstream_scope)
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
        return await deliver_callback(provider, upstream_url, iss)


async def deliver_callback(provider: AuthorizationCodeProvider, upstream_url: str, iss: str | None = None) -> str:
    """Deliver the upstream's redirect to the browser that began ``upstream_url``, against a token endpoint the caller mocks."""
    return await provider.handle_upstream_callback('upstream-code', state_in(upstream_url), BROWSER_ID, iss)


def query_value(url: str, name: str) -> str:
    """The one value of query parameter ``name`` in ``url``."""
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)[name][0]


def request_id_in(consent_url: str) -> str:
    """The sign-in a consent page URL names."""
    return query_value(consent_url, 'request')


def state_in(upstream_url: str) -> str:
    """The ``state`` the gateway sent the upstream."""
    return query_value(upstream_url, 'state')


def code_in(redirect: str) -> str:
    """The MCP authorization code carried by a redirect to the MCP client."""
    return query_value(redirect, 'code')


async def sign_in(
    provider: AuthorizationCodeProvider,
    client: OAuthClientInformationFull,
    scopes: list[str] | None = None,
    upstream_scope: str | None = None,
) -> OAuthToken:
    """Run a whole authorization through ``provider`` and return the MCP tokens it issues.

    The upstream states ``upstream_scope`` as the scope it granted, or omits it when ``None``.
    """
    redirect = await complete_upstream(
        provider, await begin_upstream(provider, client, scopes=scopes), upstream_scope=upstream_scope
    )
    auth_code = await provider.load_authorization_code(client, code_in(redirect))
    assert auth_code is not None
    return await provider.exchange_authorization_code(client, auth_code)
