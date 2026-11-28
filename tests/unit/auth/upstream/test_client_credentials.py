import asyncio
import typing

import httpx
import pytest

from openapi_mcp_gateway.auth.upstream.client_credentials import UpstreamClientCredentialsTokenSource
from tests.constants import API_URL, TOKEN_URL
from tests.token_endpoint import TokenEndpoint


class TestClientCredentialsTokenSourceFetch:
    """Initial fetch and caching behaviour of ``UpstreamClientCredentialsTokenSource``."""

    async def test_first_call_posts_client_credentials(self):
        """The first ``get_token`` POSTs ``grant_type=client_credentials`` to ``token_url``."""
        endpoint = TokenEndpoint(_token(200, {'access_token': 'fresh-token', 'expires_in': 3600}))
        source = _source(endpoint, scopes=['read', 'write'])

        token = await source.get_token()

        assert token == 'fresh-token'
        assert len(endpoint.forms) == 1
        form = endpoint.forms[0]
        assert form['grant_type'] == 'client_credentials'
        assert form['client_id'] == 'cid'
        assert form['client_secret'] == 'secret'
        assert form['scope'] == 'read write'

    async def test_second_call_uses_cached_token(self):
        """A still-fresh token is returned from cache without a second POST."""
        endpoint = TokenEndpoint(_token(200, {'access_token': 'cached-token', 'expires_in': 3600}))
        source = _source(endpoint)

        first = await source.get_token()
        second = await source.get_token()

        assert first == second == 'cached-token'
        assert len(endpoint.forms) == 1

    async def test_expired_token_triggers_refresh(self):
        """When the cached token is past the refresh skew, a fresh token is fetched."""
        endpoint = TokenEndpoint(
            _token(200, {'access_token': 'first', 'expires_in': 60}),
            _token(200, {'access_token': 'second', 'expires_in': 60}),
        )
        now = [1000.0]
        source = _source(endpoint, refresh_skew_seconds=30, clock=lambda: now[0])

        first = await source.get_token()
        # Five seconds before expiry, the cached token is inside the refresh window.
        now[0] += 55
        second = await source.get_token()

        assert first == 'first'
        assert second == 'second'
        assert len(endpoint.forms) == 2

    async def test_concurrent_callers_share_one_fetch(self):
        """N parallel ``get_token`` calls trigger exactly one upstream fetch."""

        async def slow_token(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            return _token(200, {'access_token': f'token-{len(endpoint.forms)}', 'expires_in': 3600})

        endpoint = TokenEndpoint(slow_token)
        source = _source(endpoint)

        results = await asyncio.gather(*(source.get_token() for _ in range(10)))

        assert all(result == 'token-1' for result in results)
        assert len(endpoint.forms) == 1


class TestClientCredentialsTokenSourceErrors:
    """Failure paths surface via ``RuntimeError`` so callers can handle/log."""

    async def test_non_200_raises(self):
        """A non-200 token response raises ``RuntimeError`` and does not cache anything."""
        source = _source(TokenEndpoint(httpx.Response(401, text='invalid_client')))

        with pytest.raises(RuntimeError, match='401'):
            await source.get_token()

    async def test_missing_access_token_raises(self):
        """A 200 response without ``access_token`` raises ``RuntimeError``."""
        source = _source(TokenEndpoint(_token(200, {'expires_in': 3600})))

        with pytest.raises(RuntimeError, match='access_token'):
            await source.get_token()


class TestClientCredentialsTokenSourceClose:
    """``aclose`` releases the underlying HTTP client when one was created."""

    async def test_aclose_closes_http_client(self):
        """``aclose`` closes the HTTP client a fetch opened, and a later fetch opens another."""
        endpoint = TokenEndpoint(
            _token(200, {'access_token': 'first', 'expires_in': 60}),
            _token(200, {'access_token': 'second', 'expires_in': 60}),
        )
        now = [1000.0]
        source = _source(endpoint, clock=lambda: now[0])
        await source.get_token()

        await source.aclose()

        assert endpoint.closed == 1
        now[0] += 60
        assert await source.get_token() == 'second'

    async def test_aclose_is_safe_when_unused(self):
        """``aclose`` on a never-used source is a no-op."""
        endpoint = TokenEndpoint()
        await _source(endpoint).aclose()
        assert endpoint.closed == 0


class TestClientCredentialsTokenSourceAudience:
    """Audience parameters name the API a service token is minted for."""

    async def test_audience_params_are_posted(self):
        """Configured audience parameters ride along with the client_credentials grant."""
        endpoint = TokenEndpoint(_token(200, {'access_token': 'tok', 'expires_in': 3600}))
        source = _source(endpoint, audience_params={'audience': API_URL})

        await source.get_token()

        assert endpoint.forms[0]['audience'] == API_URL

    async def test_no_audience_params_leaves_request_unchanged(self):
        """Without configuration the grant carries no audience keys at all."""
        endpoint = TokenEndpoint(_token(200, {'access_token': 'tok', 'expires_in': 3600}))
        source = _source(endpoint)

        await source.get_token()

        assert 'audience' not in endpoint.forms[0]
        assert 'resource' not in endpoint.forms[0]


def _source(endpoint: TokenEndpoint, **options: typing.Any) -> UpstreamClientCredentialsTokenSource:
    return UpstreamClientCredentialsTokenSource(
        token_url=TOKEN_URL, client_id='cid', client_secret='secret', transport=endpoint, **options
    )


def _token(status_code: int, payload: dict[str, typing.Any]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)
