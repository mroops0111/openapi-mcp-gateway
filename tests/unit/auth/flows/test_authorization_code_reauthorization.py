import asyncio
import contextlib
import typing
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.shared.auth import OAuthToken
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.flows import AuthorizationCodeReauthorizer
from openapi_mcp_gateway.auth.upstream.rejection import UpstreamRejection
from tests.constants import ISSUER
from tests.oauth import BROWSER_ID, bridge_of, query_value, sign_in, state_in, upstream_token_response


# What every sign-in in these tests is refused with, unless a test names another refusal.
EXPIRED_TOKEN = UpstreamRejection('invalid_token')


@pytest.fixture
def reauthorizer(provider) -> AuthorizationCodeReauthorizer:
    """The reauthorizer ``AuthorizationCodeFlowHandler`` builds beside the provider's Bridge."""
    bridge = bridge_of(provider)
    return AuthorizationCodeReauthorizer(
        bridge.tokens, bridge.upstream, bridge.refresher, bridge.scope_registry, f'{ISSUER}/auth/reauthorize'
    )


@pytest.fixture
async def token(provider, mcp_client_info) -> OAuthToken:
    """The MCP tokens of a user signed in through the provider."""
    return await sign_in(provider, mcp_client_info)


class TestOffer:
    """A new upstream sign-in is offered only for a token one browser and one grant stand behind."""

    async def test_no_sign_in_is_offered_for_a_token_no_browser_is_bound_to(self, reauthorizer, memory_store):
        await memory_store.set('mcp_access_token', 'mcp_unbound', _unbound_token_record(reauthorizer))

        with _calling_as('mcp_unbound'):
            assert await reauthorizer.start(EXPIRED_TOKEN) is None

    async def test_nothing_happens_outside_a_call(self, reauthorizer):
        with patch(
            'openapi_mcp_gateway.auth.flows.authorization_code_reauthorization.get_access_token', return_value=None
        ):
            assert await reauthorizer.start(EXPIRED_TOKEN) is None
            assert not await reauthorizer.renew()
            assert not await reauthorizer.wait_for_completion(0.1)

    async def test_calls_refused_together_share_one_sign_in(self, reauthorizer, token):
        """The user signs in once, asked for what every refused call needs."""
        with _calling_as(token.access_token):
            first = await reauthorizer.start(UpstreamRejection('insufficient_scope', ('read',)))
            second = await reauthorizer.start(UpstreamRejection('insufficient_scope', ('write',)))

        assert first is not None
        assert second is not None
        assert second.url == first.url
        upstream_url = await reauthorizer.begin_sign_in(second.reauthorization_id, BROWSER_ID)
        assert {'read', 'write'} <= set(query_value(upstream_url, 'scope').split())


class TestRenewal:
    """An expired upstream token is refreshed without the user."""

    async def test_a_renewal_refused_by_the_upstream_changes_nothing(self, reauthorizer, token):
        refused = MagicMock(status_code=400, text='revoked')

        with _calling_as(token.access_token), patch('httpx.AsyncClient.post', AsyncMock(return_value=refused)):
            assert not await reauthorizer.renew()

        assert await reauthorizer.tokens.load_access_token_link(token.access_token, 'upstream_access_token')

    async def test_an_unreachable_token_endpoint_renews_nothing(self, reauthorizer, token):
        unreachable = AsyncMock(side_effect=httpx.ConnectError('down'))

        with _calling_as(token.access_token), patch('httpx.AsyncClient.post', unreachable):
            assert not await reauthorizer.renew()

    async def test_calls_refused_together_spend_the_refresh_token_once(self, reauthorizer, token):
        """A rotated refresh token cannot be spent twice, so the calls after the first use its result."""
        refresh = AsyncMock(return_value=upstream_token_response('renewed'))

        with _calling_as(token.access_token), patch('httpx.AsyncClient.post', refresh):
            results = await asyncio.gather(*(reauthorizer.renew() for _ in range(3)))

        assert results == [True, True, True]
        refresh.assert_awaited_once()


class TestSignIn:
    """The sign-in runs only in the browser of the first one, and lands on the grant's current tokens."""

    async def test_a_link_naming_no_sign_in_is_refused(self, reauthorizer):
        with pytest.raises(HTTPException) as raised:
            await reauthorizer.begin_sign_in('unknown', BROWSER_ID)

        assert raised.value.status_code == 400

    async def test_a_stored_sign_in_of_another_shape_reads_as_expired(self, reauthorizer, memory_store):
        await memory_store.set('upstream_reauthorization', 'old', {'origin': reauthorizer.tokens.origin.model_dump()})

        with pytest.raises(HTTPException) as raised:
            await reauthorizer.begin_sign_in('old', BROWSER_ID)

        assert raised.value.status_code == 400

    async def test_a_finished_sign_in_cannot_run_again(self, reauthorizer, token):
        reauthorization_id, upstream_url = await _open_sign_in(reauthorizer, token)
        await _return_from_upstream(reauthorizer, upstream_url)

        with pytest.raises(HTTPException):
            await reauthorizer.begin_sign_in(reauthorization_id, BROWSER_ID)
        with _calling_as(token.access_token):
            assert await reauthorizer.wait_for_completion(0.1)

    async def test_an_upstream_refusal_ends_the_sign_in(self, reauthorizer, token):
        """The call waiting on it stops waiting."""
        _, upstream_url = await _open_sign_in(reauthorizer, token)

        with pytest.raises(HTTPException) as raised:
            await reauthorizer.handle_upstream_error('access_denied', state_in(upstream_url), BROWSER_ID)

        assert 'access_denied' in raised.value.detail
        with _calling_as(token.access_token):
            assert not await reauthorizer.wait_for_completion(0.1)

    async def test_a_sign_in_for_a_revoked_token_fails(self, reauthorizer, token):
        _, upstream_url = await _open_sign_in(reauthorizer, token)
        await reauthorizer.tokens.revoke_access_token(token.access_token)

        with pytest.raises(HTTPException) as raised:
            await _return_from_upstream(reauthorizer, upstream_url)

        assert 'has ended' in raised.value.detail

    async def test_a_callback_under_an_unknown_state_is_refused(self, reauthorizer):
        with pytest.raises(HTTPException):
            await reauthorizer.handle_upstream_callback('upstream-code', 'unknown', BROWSER_ID)

    async def test_a_sign_in_reaches_the_tokens_a_refresh_rotated_to_meanwhile(
        self, provider, mcp_client_info, reauthorizer, token
    ):
        """The sign-in follows its grant, so a client refreshing while the user signs in loses nothing."""
        _, upstream_url = await _open_sign_in(reauthorizer, token)
        assert token.refresh_token is not None
        refresh = await provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None
        rotated = await provider.exchange_refresh_token(mcp_client_info, refresh, [])

        await _return_from_upstream(reauthorizer, upstream_url)

        assert (
            await reauthorizer.tokens.load_access_token_link(rotated.access_token, 'upstream_access_token') == 'again'
        )
        with _calling_as(rotated.access_token):
            assert await reauthorizer.wait_for_completion(0.1)

    async def test_a_sign_in_without_a_new_refresh_token_keeps_the_old_one(self, reauthorizer, token):
        """Some upstreams send a refresh token on first consent only."""
        _, upstream_url = await _open_sign_in(reauthorizer, token)
        no_refresh_token = upstream_token_response('again')
        del no_refresh_token.json.return_value['refresh_token']

        await _return_from_upstream(reauthorizer, upstream_url, no_refresh_token)

        attachment = await reauthorizer.tokens.load_access_token_attachment(token.access_token)
        assert attachment is not None
        assert attachment.links == {
            'upstream_access_token': 'again',
            'upstream_refresh_token': 'upstream-refresh-token',
        }


def _calling_as(mcp_access_token: str) -> contextlib.AbstractContextManager:
    """Handle the rest of the block as a call made with ``mcp_access_token``."""
    access_token = MagicMock(token=mcp_access_token, scopes=['api'])
    return patch(
        'openapi_mcp_gateway.auth.flows.authorization_code_reauthorization.get_access_token', return_value=access_token
    )


async def _open_sign_in(reauthorizer: AuthorizationCodeReauthorizer, token: OAuthToken) -> tuple[str, str]:
    """Offer a new sign-in for ``token`` and open its link in the first sign-in's browser.

    Returns the sign-in's id and the upstream authorize URL the browser is sent to.
    """
    with _calling_as(token.access_token):
        reauthorization = await reauthorizer.start(EXPIRED_TOKEN)
    assert reauthorization is not None
    upstream_url = await reauthorizer.begin_sign_in(reauthorization.reauthorization_id, BROWSER_ID)
    return reauthorization.reauthorization_id, upstream_url


async def _return_from_upstream(
    reauthorizer: AuthorizationCodeReauthorizer, upstream_url: str, granted: MagicMock | None = None
) -> None:
    """The upstream sends the browser back, granting ``granted`` or else the token ``again``."""
    response = granted or upstream_token_response('again')
    with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
        await reauthorizer.handle_upstream_callback('upstream-code', state_in(upstream_url), BROWSER_ID)


def _unbound_token_record(reauthorizer: AuthorizationCodeReauthorizer) -> dict[str, typing.Any]:
    """An MCP access token record of this server that carries no browser and no grant."""
    return {
        'token': 'mcp_unbound',
        'client_id': 'client-1',
        'scopes': ['read'],
        'expires_at': 9999999999,
        'origin': reauthorizer.tokens.origin.model_dump(),
    }
