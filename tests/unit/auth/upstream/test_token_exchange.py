import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openapi_mcp_gateway.auth.upstream.token_exchange import UpstreamTokenExchangeClient
from tests.constants import API_URL, TOKEN_URL


def _build_response(status_code: int, payload: dict | None = None, text: str = '') -> MagicMock:
    """Construct a MagicMock that mimics ``httpx.Response`` enough for the source."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.text = text
    return response


class TestUpstreamTokenExchangeClient:
    """RFC 8693 exchange of a caller's token for one the upstream API accepts."""

    def _source(self, **kwargs) -> UpstreamTokenExchangeClient:
        defaults = {
            'token_endpoint': TOKEN_URL,
            'client_id': 'gateway',
            'client_secret': 'secret',
            'audience_params': {'audience': API_URL},
        }
        return UpstreamTokenExchangeClient(**{**defaults, **kwargs})

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
        assert posted['audience'] == API_URL
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
        source = self._source(refresh_skew_seconds=5.0)
        responses = [
            _build_response(200, {'access_token': 'first', 'expires_in': 1}),
            _build_response(200, {'access_token': 'second', 'expires_in': 3600}),
        ]
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(side_effect=responses)

        assert await source.exchange('caller-token') == 'first'
        assert await source.exchange('caller-token') == 'second'

    async def test_an_expired_entry_is_dropped_on_the_next_exchange(self):
        """Callers whose tokens rotate do not grow the cache without bound."""
        source = self._source(refresh_skew_seconds=5.0)
        responses = [
            _build_response(200, {'access_token': 'first', 'expires_in': 1}),
            _build_response(200, {'access_token': 'second', 'expires_in': 3600}),
        ]
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(side_effect=responses)

        await source.exchange('old-caller-token')
        with patch('time.monotonic', return_value=time.monotonic() + 10):
            await source.exchange('new-caller-token')

        assert set(source._cache) == {'new-caller-token'}

    async def test_failure_explains_the_likely_causes(self):
        """A rejected exchange names what to check, since support varies between authorization servers."""
        source = self._source()
        source._http_client = MagicMock()
        source._http_client.post = AsyncMock(return_value=_build_response(400, text='unsupported_grant_type'))

        with pytest.raises(RuntimeError, match='RFC 8693'):
            await source.exchange('caller-token')
