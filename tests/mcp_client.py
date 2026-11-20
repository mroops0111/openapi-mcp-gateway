import contextlib
import typing
import urllib.parse

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider, OAuthFlowError, TokenStorage
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from tests.oauth import csrf_token_in, query_value


# Where an MCP client asks to be sent back once authorized, which it never serves since the test plays the browser.
CLIENT_CALLBACK = 'http://127.0.0.1:3030/callback'


@contextlib.asynccontextmanager
async def connect(endpoint: str, auth: httpx2.Auth, **options: typing.Any) -> typing.AsyncIterator[Client]:
    """An MCP SDK client of ``endpoint`` signed in through ``auth``, built with ``options`` such as ``mode``."""
    async with (
        httpx2.AsyncClient(auth=auth) as http_client,
        Client(streamable_http_client(endpoint, http_client=http_client), **options) as client,
    ):
        yield client


def authorization_code_auth(
    endpoint: str,
    browser: 'Browser',
    client_id: str | None = None,
    scope: str | None = None,
    storage: 'MemoryTokenStorage | None' = None,
    client_metadata_url: str | None = None,
) -> OAuthClientProvider:
    """The SDK's own authorization code sign-in at ``endpoint``, with ``browser`` playing the user.

    A ``client_id`` names a client the gateway already knows.
    Without one the client signs in as ``client_metadata_url`` where the gateway accepts that,
    and registers itself otherwise.
    """
    storage = storage or MemoryTokenStorage()
    if client_id is not None:
        storage.client_info = OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=[CLIENT_CALLBACK],  # type: ignore[list-item]
            grant_types=['authorization_code', 'refresh_token'],
            token_endpoint_auth_method='none',
        )
    return OAuthClientProvider(
        server_url=endpoint,
        client_metadata=OAuthClientMetadata(redirect_uris=[CLIENT_CALLBACK], client_name='agent', scope=scope),  # type: ignore[list-item]
        storage=storage,
        redirect_handler=browser.sign_in,
        callback_handler=browser.return_to_client,
        client_metadata_url=client_metadata_url,
    )


def client_credentials_auth(
    endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str | None = None,
    storage: TokenStorage | None = None,
    issuer: str | None = None,
) -> ClientCredentialsOAuthProvider:
    """The SDK's own client_credentials sign-in at ``endpoint``, at ``issuer`` or else the gateway serving it."""
    if issuer is None:
        gateway_issuer = endpoint.removesuffix('/mcp')
        # The gateway publishes a root issuer with its trailing slash, as ``pydantic.AnyHttpUrl`` renders it.
        issuer = gateway_issuer if urllib.parse.urlparse(gateway_issuer).path else f'{gateway_issuer}/'
    return ClientCredentialsOAuthProvider(
        server_url=endpoint,
        storage=storage or MemoryTokenStorage(),
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        issuer=issuer,
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


class Browser:
    """The user's browser, which approves the client where asked and plays the upstream sending the user back.

    ``issuer`` is the gateway's issuer for the server, under which its consent and callback pages sit.
    ``expect_consent`` says whether the gateway must show the consent page, which a sign-in fails on otherwise.
    """

    def __init__(self, issuer: str, expect_consent: bool = True) -> None:
        self.issuer = issuer.rstrip('/')
        self.expect_consent = expect_consent
        self._client_redirect: str | None = None

    async def sign_in(self, authorization_url: str) -> None:
        async with httpx2.AsyncClient(follow_redirects=False) as browser:
            location = (await browser.get(authorization_url)).headers['location']
            shows_consent = location.startswith(f'{self.issuer}/auth/consent')
            assert shows_consent == self.expect_consent, f'consent page shown: {shows_consent}'
            if shows_consent:
                page = await browser.get(location)
                decision = {'csrf_token': csrf_token_in(page.text), 'decision': 'approve'}
                location = (await browser.post(location, data=decision)).headers['location']
            if not location.startswith(CLIENT_CALLBACK):
                # The upstream signs the user in and sends them back to the gateway's callback.
                callback = await browser.get(
                    f'{self.issuer}/auth/callback',
                    params={'code': 'upstream-code', 'state': query_value(location, 'state')},
                )
                location = callback.headers['location']
            self._client_redirect = location

    async def return_to_client(self) -> AuthorizationCodeResult:
        """What the redirect to the client carries, ``iss`` included, which the SDK checks per RFC 9207."""
        assert self._client_redirect is not None
        parameters = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self._client_redirect).query))
        if 'error' in parameters:
            raise OAuthFlowError(f'{parameters["error"]}: {parameters.get("error_description", "")}')
        return AuthorizationCodeResult(
            code=parameters['code'], state=parameters.get('state'), iss=parameters.get('iss')
        )
