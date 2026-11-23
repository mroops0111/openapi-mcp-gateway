import contextlib
import pathlib
import typing
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import httpx2
import pytest
from mcp import MCPError, UrlElicitationRequiredError
from mcp.client import ClientRequestContext
from mcp.types import CallToolResult, ElicitRequestParams, ElicitRequestURLParams, ElicitResult

from openapi_mcp_gateway.exposure import _upstream
from tests.mcp_client import Browser, authorization_code_auth, connect
from tests.oauth import authorization_code_gateway, query_value, upstream_token_response
from tests.serving import serve_gateway
from tests.tool_result import text_of


# The pre-registered client every test signs in as.
CLIENT = 'agent'

# The upstream token the first sign-in grants, and the one each later sign-in or renewal grants.
FIRST_TOKEN = 'upstream-token-1'
SECOND_TOKEN = 'upstream-token-2'

# The challenges an upstream refuses a token with.
INSUFFICIENT_SCOPE = 'Bearer realm="pets", error="insufficient_scope", scope="write"'
INVALID_TOKEN = 'Bearer realm="pets", error="invalid_token", error_description="The token expired"'

# A protocol revision older than input requests in results.
LEGACY = 'legacy'

# The answer a client gives each URL elicitation, and the browser that opens it, if any.
Elicitation = typing.Callable[[ElicitRequestURLParams], typing.Awaitable[ElicitResult]]


class UpstreamTokenEndpoint:
    """The upstream token endpoint, granting a new numbered token on each exchange or refresh."""

    def __init__(self) -> None:
        self.grant_types: list[str] = []

    def __call__(self, url: str, data: dict[str, str], **_: typing.Any) -> MagicMock:
        self.grant_types.append(data['grant_type'])
        return upstream_token_response(granted=f'upstream-token-{len(self.grant_types)}')


@pytest.fixture
def token_endpoint() -> typing.Iterator[UpstreamTokenEndpoint]:
    endpoint = UpstreamTokenEndpoint()
    with patch('httpx.AsyncClient.post', AsyncMock(side_effect=endpoint)):
        yield endpoint


@pytest.fixture
def upstream_bearers(mock_upstream) -> typing.Callable[[str], list[str]]:
    """Install an upstream that accepts only ``SECOND_TOKEN``, refusing others with ``challenge``.

    Returns the installer, which returns the bearer of every upstream request in order.
    """

    def install(challenge: str) -> list[str]:
        bearers: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bearer = request.headers['authorization'].removeprefix('Bearer ')
            bearers.append(bearer)
            if bearer == SECOND_TOKEN:
                return httpx.Response(200, json=[{'id': 1, 'name': 'fido'}])
            status = 401 if 'invalid_token' in challenge else 403
            return httpx.Response(status, headers={'WWW-Authenticate': challenge}, json={'message': 'refused'})

        mock_upstream(handler)
        return bearers

    return install


class TestNarrowGrant:
    """An upstream that says the token lacks a scope sends the user to sign in again, and the call goes on."""

    async def test_the_call_goes_on_after_the_user_signs_in_again(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        bearers = upstream_bearers(INSUFFICIENT_SCOPE)
        elicitations: list[ElicitRequestURLParams] = []
        async with _signed_in(petstore_json_path) as (base_url, browser, call):

            async def open_in_the_same_browser(params: ElicitRequestURLParams) -> ElicitResult:
                elicitations.append(params)
                page = await browser.sign_in_again(params.url)
                assert page.status_code == 200, page.text
                return ElicitResult(action='accept')

            result = await call(open_in_the_same_browser)

        assert not result.is_error, text_of(result)
        assert bearers == [FIRST_TOKEN, SECOND_TOKEN]
        assert elicitations[0].url.startswith(f'{base_url}/petstore/auth/reauthorize?id=')
        assert 'write' in query_value(browser.upstream_sign_ins[-1], 'scope').split()

    async def test_a_declined_sign_in_returns_the_upstream_error(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        bearers = upstream_bearers(INSUFFICIENT_SCOPE)
        async with _signed_in(petstore_json_path) as (_, _, call):
            result = await call(_answer('decline'))

        assert result.is_error
        assert '403' in text_of(result)
        assert bearers == [FIRST_TOKEN, FIRST_TOKEN]

    async def test_another_browser_cannot_finish_the_sign_in(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        """Only the browser of the first sign-in may finish this one, which keeps a phished link useless."""
        upstream_bearers(INSUFFICIENT_SCOPE)
        pages: list[httpx2.Response] = []
        async with _signed_in(petstore_json_path) as (base_url, _, call):

            async def open_in_another_browser(params: ElicitRequestURLParams) -> ElicitResult:
                pages.append(await Browser(f'{base_url}/petstore').sign_in_again(params.url))
                return ElicitResult(action='decline')

            result = await call(open_in_another_browser)

        assert result.is_error
        assert pages[0].status_code == 403
        assert 'browser you first signed in with' in pages[0].text

    async def test_a_scope_beyond_the_ceiling_returns_the_upstream_error(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        """A sign-in may not ask the upstream for more than ``auth.upstream.scopes``, so none is offered."""
        upstream_bearers(INSUFFICIENT_SCOPE.replace('write', 'admin'))
        async with _signed_in(petstore_json_path) as (_, _, call):
            result = await call(_never_asked)

        assert result.is_error

    async def test_a_client_that_cannot_open_a_url_gets_the_upstream_error(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        upstream_bearers(INSUFFICIENT_SCOPE)
        async with _signed_in(petstore_json_path) as (_, _, call):
            result = await call(None)

        assert result.is_error
        assert '403' in text_of(result)


class TestExpiredToken:
    """An upstream that says the token expired gets it renewed, without the user."""

    async def test_the_token_is_renewed_and_the_call_goes_on(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        bearers = upstream_bearers(INVALID_TOKEN)
        async with _signed_in(petstore_json_path) as (_, _, call):
            result = await call(_never_asked)

        assert not result.is_error, text_of(result)
        assert bearers == [FIRST_TOKEN, SECOND_TOKEN]
        assert token_endpoint.grant_types == ['authorization_code', 'refresh_token']


class TestLegacyClient:
    """A 2025-11-25 client gets ``URLElicitationRequiredError``, and retries by hand once the user signed in."""

    async def test_the_retry_goes_on_after_the_user_signs_in_again(
        self, petstore_json_path, token_endpoint, upstream_bearers
    ):
        bearers = upstream_bearers(INSUFFICIENT_SCOPE)
        async with _signed_in(petstore_json_path, mode=LEGACY) as (_, browser, call):
            with pytest.raises(MCPError) as raised:
                await call(_never_asked)
            required = UrlElicitationRequiredError.from_error(raised.value.error)
            assert required.elicitations[0].elicitation_id
            page = await browser.sign_in_again(required.elicitations[0].url)
            assert page.status_code == 200, page.text
            result = await call(_never_asked)

        assert not result.is_error, text_of(result)
        assert bearers == [FIRST_TOKEN, SECOND_TOKEN]


@pytest.fixture(autouse=True)
def short_sign_in_wait(monkeypatch):
    """Keep a retried call from waiting long for a sign-in no browser finishes."""
    monkeypatch.setattr(_upstream, 'SIGN_IN_WAIT_SECONDS', 0.5)


@contextlib.asynccontextmanager
async def _signed_in(
    spec: pathlib.Path, mode: str = 'auto'
) -> typing.AsyncIterator[tuple[str, Browser, typing.Callable[[Elicitation | None], typing.Awaitable[CallToolResult]]]]:
    """Sign ``CLIENT`` in with the SDK's own client, and yield a way to call ``list_pets`` as it.

    The upstream may be asked for ``read`` and ``write``.
    Each call takes how the client answers a URL elicitation, or ``None`` for a client that declares none.
    """

    def build(url: str):
        return authorization_code_gateway(spec, {CLIENT: {}}, upstream={'scopes': ['read', 'write']}, url=url)

    async with serve_gateway(build) as base_url:
        endpoint = f'{base_url}/petstore/mcp'
        browser = Browser(f'{base_url}/petstore')
        auth = authorization_code_auth(endpoint, browser, CLIENT)

        async def call(elicitation: Elicitation | None) -> CallToolResult:
            options: dict[str, typing.Any] = {'mode': mode}
            if elicitation is not None:

                async def callback(_: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
                    assert isinstance(params, ElicitRequestURLParams)
                    return await elicitation(params)

                options['elicitation_callback'] = callback
            # A protocol error is raised once the client has closed, so it is not wrapped in the client's task group.
            async with connect(endpoint, auth, **options) as client:
                try:
                    return await client.call_tool('list_pets', {})
                except MCPError as error:
                    refusal = error
            raise refusal

        yield base_url, browser, call


def _answer(action: typing.Literal['accept', 'decline', 'cancel']) -> Elicitation:
    async def answer(_: ElicitRequestURLParams) -> ElicitResult:
        return ElicitResult(action=action)

    return answer


async def _never_asked(params: ElicitRequestURLParams) -> ElicitResult:
    raise AssertionError(f'no sign-in should be offered, got {params.url}')
