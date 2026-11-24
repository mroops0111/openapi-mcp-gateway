import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException

from openapi_mcp_gateway.auth.upstream import authorization_code_refresh
from openapi_mcp_gateway.auth.upstream.authorization_code_refresh import UpstreamTokenRefresher
from tests.oauth import bridge_of, upstream_token_response


# The upstream refresh token every test spends.
REFRESH_TOKEN = 'upstream-refresh-token'


@pytest.fixture
def gateway_instances(provider, memory_store) -> list[UpstreamTokenRefresher]:
    """Two gateway instances' refreshers, sharing one store as instances behind a load balancer do."""
    upstream = bridge_of(provider).upstream
    return [UpstreamTokenRefresher(upstream, memory_store), UpstreamTokenRefresher(upstream, memory_store)]


class TestUpstreamTokenRefresher:
    """A refresh token is spent once, however many callers in however many instances ask."""

    async def test_callers_in_every_instance_share_one_refresh(self, gateway_instances):
        token_endpoint = AsyncMock(return_value=upstream_token_response('renewed'))

        with patch('httpx.AsyncClient.post', token_endpoint):
            answers = await asyncio.gather(
                *(refresher.refresh(REFRESH_TOKEN) for refresher in gateway_instances for _ in range(2))
            )

        token_endpoint.assert_awaited_once()
        assert {answer.access_token for answer in answers} == {'renewed'}

    async def test_a_failed_refresh_lets_the_next_caller_try(self, gateway_instances):
        refused = MagicMock(status_code=400, text='temporarily unavailable')
        token_endpoint = AsyncMock(side_effect=[refused, upstream_token_response('renewed')])

        with patch('httpx.AsyncClient.post', token_endpoint):
            with pytest.raises(HTTPException):
                await gateway_instances[0].refresh(REFRESH_TOKEN)
            answer = await gateway_instances[1].refresh(REFRESH_TOKEN)

        assert answer.access_token == 'renewed'

    async def test_a_claim_left_by_a_dead_instance_expires(self, gateway_instances, memory_store, monkeypatch):
        """An instance that died mid-refresh holds the others up only until its claim expires."""
        monkeypatch.setattr(authorization_code_refresh, '_REFRESH_CLAIM_SECONDS', 1)
        # Claims are keyed by the refresh token's hash, as the refresher keys them.
        claim_key = hashlib.sha256(REFRESH_TOKEN.encode()).hexdigest()
        await memory_store.set_if_absent('upstream_refresh_claim', claim_key, True, ttl=1)

        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response('renewed'))):
            answer = await gateway_instances[0].refresh(REFRESH_TOKEN)

        assert answer.access_token == 'renewed'
