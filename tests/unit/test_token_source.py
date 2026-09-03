import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.token_source import ClientCredentialsTokenSource, TokenExchangeTokenSource


def _build_response(status_code: int, payload: dict | None = None, text: str = '') -> MagicMock:
    """Construct a MagicMock that mimics ``httpx.Response`` enough for the source."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.text = text
    return response


class TestClientCredentialsTokenSourceFetch:
    """Initial fetch and caching behaviour of ``ClientCredentialsTokenSource``."""

    async def test_first_call_posts_client_credentials(self):
        """The first ``get_token`` POSTs ``grant_type=client_credentials`` to ``token_url``."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
            scopes=['read', 'write'],
        )
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'fresh-token', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        token = await source.get_token()

        assert token == 'fresh-token'
        post_mock.assert_awaited_once()
        assert post_mock.await_args is not None
        call_kwargs = post_mock.await_args.kwargs
        assert call_kwargs['data']['grant_type'] == 'client_credentials'
        assert call_kwargs['data']['client_id'] == 'cid'
        assert call_kwargs['data']['client_secret'] == 'secret'
        assert call_kwargs['data']['scope'] == 'read write'

    async def test_second_call_uses_cached_token(self):
        """A still-fresh token is returned from cache without a second POST."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'cached-token', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        first = await source.get_token()
        second = await source.get_token()

        assert first == second == 'cached-token'
        post_mock.assert_awaited_once()

    async def test_expired_token_triggers_refresh(self):
        """When the cached token is past the refresh skew, a fresh token is fetched."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
            refresh_skew_seconds=30,
        )
        responses = [
            _build_response(200, {'access_token': 'first', 'expires_in': 60}),
            _build_response(200, {'access_token': 'second', 'expires_in': 60}),
        ]
        post_mock = AsyncMock(side_effect=responses)
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        first = await source.get_token()
        # Force the cached token into the refresh window.
        source._expires_at_monotonic = time.monotonic() + 5
        second = await source.get_token()

        assert first == 'first'
        assert second == 'second'
        assert post_mock.await_count == 2

    async def test_concurrent_callers_share_one_fetch(self):
        """N parallel ``get_token`` calls trigger exactly one upstream fetch."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )

        call_counter = {'n': 0}

        async def slow_post(*_args, **_kwargs):
            call_counter['n'] += 1
            await asyncio.sleep(0.01)
            return _build_response(200, {'access_token': f'token-{call_counter["n"]}', 'expires_in': 3600})

        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(side_effect=slow_post)

        results = await asyncio.gather(*(source.get_token() for _ in range(10)))

        assert all(result == 'token-1' for result in results)
        assert call_counter['n'] == 1


class TestClientCredentialsTokenSourceErrors:
    """Failure paths surface via ``RuntimeError`` so callers can handle/log."""

    async def test_non_200_raises(self):
        """A non-200 token response raises ``RuntimeError`` and does not cache anything."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(return_value=_build_response(401, payload={}, text='invalid_client'))

        with pytest.raises(RuntimeError, match='401'):
            await source.get_token()

    async def test_missing_access_token_raises(self):
        """A 200 response without ``access_token`` raises ``RuntimeError``."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(return_value=_build_response(200, payload={'expires_in': 3600}))

        with pytest.raises(RuntimeError, match='access_token'):
            await source.get_token()


class TestClientCredentialsTokenSourceClose:
    """``aclose`` releases the underlying HTTP client when one was created."""

    async def test_aclose_closes_http_client(self):
        """``aclose`` calls ``aclose`` on the internal ``httpx.AsyncClient`` if present."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        client_mock = MagicMock()
        client_mock.aclose = AsyncMock()
        source._http_client = client_mock

        await source.aclose()

        client_mock.aclose.assert_awaited_once()
        assert source._http_client is None

    async def test_aclose_is_safe_when_unused(self):
        """``aclose`` on a never-used source is a no-op."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        await source.aclose()  # Should not raise.


class TestClientCredentialsTokenSourceAudience:
    """Audience parameters name the API a service token is minted for."""

    async def test_audience_params_are_posted(self):
        """Configured audience parameters ride along with the client_credentials grant."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
            audience_params={'audience': 'https://api.example.com'},
        )
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'tok', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        await source.get_token()

        assert post_mock.await_args is not None
        assert post_mock.await_args.kwargs['data']['audience'] == 'https://api.example.com'

    async def test_no_audience_params_leaves_request_unchanged(self):
        """Without configuration the grant carries no audience keys at all."""
        source = ClientCredentialsTokenSource(
            token_url='https://auth.example.com/token',
            client_id='cid',
            client_secret='secret',
        )
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'tok', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        await source.get_token()

        assert post_mock.await_args is not None
        assert 'audience' not in post_mock.await_args.kwargs['data']
        assert 'resource' not in post_mock.await_args.kwargs['data']


class TestTokenExchangeTokenSource:
    """RFC 8693 exchange of a caller's token for one the upstream API accepts."""

    def _source(self, **kwargs) -> TokenExchangeTokenSource:
        defaults = {
            'token_endpoint': 'https://auth.example.com/token',
            'client_id': 'gateway',
            'client_secret': 'secret',
            'audience_params': {'audience': 'https://api.example.com'},
        }
        return TokenExchangeTokenSource(**{**defaults, **kwargs})

    async def test_posts_rfc_8693_grant(self):
        """The request carries the token-exchange grant, the subject token, and the target audience."""
        source = self._source()
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'upstream', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        token = await source.exchange('caller-token')

        assert token == 'upstream'
        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert posted['grant_type'] == 'urn:ietf:params:oauth:grant-type:token-exchange'
        assert posted['subject_token'] == 'caller-token'
        assert posted['subject_token_type'] == 'urn:ietf:params:oauth:token-type:access_token'
        assert posted['audience'] == 'https://api.example.com'
        assert posted['client_id'] == 'gateway'

    async def test_repeat_exchange_is_cached(self):
        """One MCP session makes many tool calls, so a fresh result is reused."""
        source = self._source()
        post_mock = AsyncMock(return_value=_build_response(200, {'access_token': 'upstream', 'expires_in': 3600}))
        source._http_client = MagicMock()
        source._http_client.post = post_mock

        first = await source.exchange('caller-token')
        second = await source.exchange('caller-token')

        assert first == second == 'upstream'
        post_mock.assert_awaited_once()

    async def test_different_subject_tokens_do_not_share_a_result(self):
        """Caching is keyed by the subject token, so a re-authenticated caller gets a fresh exchange.

        Keying by user instead would let a new session inherit credentials minted for an old one.
        """
        source = self._source()
        responses = [
            _build_response(200, {'access_token': 'upstream-a', 'expires_in': 3600}),
            _build_response(200, {'access_token': 'upstream-b', 'expires_in': 3600}),
        ]
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(side_effect=responses)

        assert await source.exchange('caller-a') == 'upstream-a'
        assert await source.exchange('caller-b') == 'upstream-b'

    async def test_expired_cache_entry_triggers_a_new_exchange(self):
        """A result past its refresh skew is discarded rather than returned stale."""
        source = self._source(refresh_skew_seconds=0.0)
        responses = [
            _build_response(200, {'access_token': 'first', 'expires_in': 0}),
            _build_response(200, {'access_token': 'second', 'expires_in': 3600}),
        ]
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(side_effect=responses)

        assert await source.exchange('caller-token') == 'first'
        assert await source.exchange('caller-token') == 'second'

    async def test_failure_explains_the_likely_causes(self):
        """A rejected exchange names what to check, since support varies between authorization servers."""
        source = self._source()
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(return_value=_build_response(400, text='unsupported_grant_type'))

        with pytest.raises(RuntimeError, match='RFC 8693'):
            await source.exchange('caller-token')
