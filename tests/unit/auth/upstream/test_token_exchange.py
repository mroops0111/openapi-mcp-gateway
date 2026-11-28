import typing

import httpx
import pytest

from openapi_mcp_gateway.auth.upstream.token_exchange import (
    UpstreamTokenExchangeClient,
    UpstreamTokenExchangeError,
    UpstreamTokenExchangeRefusedError,
)
from tests.constants import API_URL, TOKEN_URL
from tests.token_endpoint import TokenEndpoint


# The upstream scopes each exchange asks for.
ASKED = ['products.read', 'products.write']


class TestUpstreamTokenExchangeClient:
    """RFC 8693 exchange of a caller's token for one the upstream API accepts."""

    async def test_posts_rfc_8693_grant(self):
        """The request carries the token-exchange grant, the subject token, and the target audience."""
        source, endpoint = _exchange_client(_build_response(200, {'access_token': 'upstream', 'expires_in': 3600}))

        exchanged = await source.exchange('caller-token', ASKED)

        assert exchanged.access_token == 'upstream'
        posted = endpoint.forms[-1]
        assert posted['grant_type'] == 'urn:ietf:params:oauth:grant-type:token-exchange'
        assert posted['subject_token'] == 'caller-token'
        assert posted['subject_token_type'] == 'urn:ietf:params:oauth:token-type:access_token'
        assert posted['audience'] == API_URL
        assert posted['client_id'] == 'gateway'
        assert posted['scope'] == 'products.read products.write'


class TestExchangeCache:
    """One MCP session makes many tool calls, so a fresh result is reused for the same subject token and scopes."""

    async def test_repeat_exchange_is_cached(self):
        source, endpoint = _exchange_client(_build_response(200, {'access_token': 'upstream', 'expires_in': 3600}))

        first = (await source.exchange('caller-token', ASKED)).access_token
        second = (await source.exchange('caller-token', ASKED)).access_token

        assert first == second == 'upstream'
        assert len(endpoint.forms) == 1

    async def test_different_subject_tokens_do_not_share_a_result(self):
        """Keying by user instead would let a new session inherit credentials minted for an old one."""
        source, _ = _exchange_client(
            _build_response(200, {'access_token': 'upstream-a', 'expires_in': 3600}),
            _build_response(200, {'access_token': 'upstream-b', 'expires_in': 3600}),
        )

        assert (await source.exchange('caller-a', ASKED)).access_token == 'upstream-a'
        assert (await source.exchange('caller-b', ASKED)).access_token == 'upstream-b'

    async def test_different_scopes_do_not_share_a_result(self):
        """A token exchanged for fewer scopes would refuse a call the wider exchange opens."""
        source, _ = _exchange_client(
            _build_response(200, {'access_token': 'narrow', 'expires_in': 3600}),
            _build_response(200, {'access_token': 'wide', 'expires_in': 3600}),
        )

        assert (await source.exchange('caller-token', ['products.read'])).access_token == 'narrow'
        assert (await source.exchange('caller-token', ASKED)).access_token == 'wide'

    async def test_expired_cache_entry_triggers_a_new_exchange(self):
        """A result past its renewal time is discarded rather than returned stale."""
        now = [1000.0]
        source, _ = _exchange_client(
            _build_response(200, {'access_token': 'first', 'expires_in': 10}),
            _build_response(200, {'access_token': 'second', 'expires_in': 3600}),
            refresh_skew_seconds=5.0,
            clock=lambda: now[0],
        )

        assert (await source.exchange('caller-token', ASKED)).access_token == 'first'
        now[0] += 6
        assert (await source.exchange('caller-token', ASKED)).access_token == 'second'

    async def test_a_lifetime_shorter_than_the_skew_is_still_reused_for_half_of_it(self):
        """Otherwise every call made with such a token would exchange it again."""
        source, endpoint = _exchange_client(
            _build_response(200, {'access_token': 'short', 'expires_in': 20}), refresh_skew_seconds=30.0
        )

        await source.exchange('caller-token', ASKED)
        await source.exchange('caller-token', ASKED)

        assert len(endpoint.forms) == 1


class TestExchangeFailure:
    """A refusal of the scopes asked for is the caller's answer, and any other failure is the operator's."""

    @pytest.mark.parametrize('error_code', ['invalid_scope', 'invalid_target'])
    async def test_a_refused_scope_or_target_names_the_scopes_and_not_the_response(self, error_code):
        source, _ = _exchange_client(
            _build_response(400, {'error': error_code, 'error_description': 'internal detail'}, text='internal detail')
        )

        with pytest.raises(UpstreamTokenExchangeRefusedError) as raised:
            await source.exchange('caller-token', ASKED)

        assert 'products.read products.write' in str(raised.value)
        assert error_code in str(raised.value)
        assert 'internal detail' not in str(raised.value)

    @pytest.mark.parametrize(
        ('status_code', 'payload'),
        [(400, {'error': 'unsupported_grant_type'}), (401, {'error': 'invalid_client'}), (503, None)],
        ids=['unsupported grant', 'bad client secret', 'issuer down'],
    )
    async def test_any_other_failure_explains_the_likely_causes(self, status_code, payload):
        """Support for the grant varies between authorization servers, so the error names what to check."""
        source, _ = _exchange_client(_build_response(status_code, payload, text='detail'))

        with pytest.raises(UpstreamTokenExchangeError, match='RFC 8693') as raised:
            await source.exchange('caller-token', ASKED)

        assert not isinstance(raised.value, UpstreamTokenExchangeRefusedError)

    async def test_a_response_that_is_not_json_is_a_failed_exchange(self):
        source, _ = _exchange_client(httpx.Response(200, content=b'not JSON'))

        with pytest.raises(UpstreamTokenExchangeError, match='not JSON'):
            await source.exchange('caller-token', ASKED)


class TestExchangedScopes:
    """What the issuer granted on an exchanged token, which a call's upstream grant is checked against."""

    @pytest.mark.parametrize(
        ('payload', 'granted'),
        [
            ({'access_token': 'upstream', 'scope': 'products.read'}, ('products.read',)),
            ({'access_token': 'upstream'}, ('products.read', 'products.write')),
        ],
        ids=['narrowed by the issuer', 'left out, so as asked'],
    )
    async def test_the_granted_scopes_come_from_the_response(self, payload, granted):
        """RFC 8693 §2.2.1 lets a response leave ``scope`` out when it equals what was asked for."""
        source, _ = _exchange_client(_build_response(200, payload))

        assert (await source.exchange('caller-token', ASKED)).scopes == granted

    async def test_no_scope_is_sent_when_none_is_asked_for(self):
        """The issuer then applies its own default, as an ``authorization_code`` sign-in leaves it to the upstream."""
        source, endpoint = _exchange_client(_build_response(200, {'access_token': 'upstream'}))

        await source.exchange('caller-token', [])

        assert 'scope' not in endpoint.forms[-1]


def _exchange_client(
    *responses: httpx.Response, **overrides: typing.Any
) -> tuple[UpstreamTokenExchangeClient, TokenEndpoint]:
    """An exchange client of the gateway against ``TOKEN_URL``, naming the API as its audience.

    Its token endpoint, returned beside it, answers with ``responses`` in turn.
    """
    endpoint = TokenEndpoint(*responses)
    defaults: dict[str, typing.Any] = {
        'token_endpoint': TOKEN_URL,
        'client_id': 'gateway',
        'client_secret': 'secret',
        'audience_params': {'audience': API_URL},
        'transport': endpoint,
    }
    return UpstreamTokenExchangeClient(**{**defaults, **overrides}), endpoint


def _build_response(status_code: int, payload: dict | None = None, text: str = '') -> httpx.Response:
    """A token endpoint's answer, JSON when ``payload`` is given and ``text`` otherwise."""
    if payload is not None:
        return httpx.Response(status_code, json=payload)
    return httpx.Response(status_code, text=text)
