import dataclasses
import hashlib
import logging

import anyio
from starlette.exceptions import HTTPException

from ...stores.base import TokenStore
from .authorization_code import UpstreamAuthorizationCodeClient, UpstreamTokenResponse


logger = logging.getLogger(__name__)

# Where one refresh of an upstream refresh token is claimed, and where its answer waits for the other callers.
# Both are keyed by a hash of the refresh token, so no token is written into a key.
UPSTREAM_REFRESH_CLAIM_NAMESPACE = 'upstream_refresh_claim'
UPSTREAM_REFRESH_ANSWER_NAMESPACE = 'upstream_refresh_answer'


# How long one caller may hold the claim before another may take it, in seconds,
# which bounds what a gateway instance that died mid-refresh holds up.
_REFRESH_CLAIM_SECONDS = 30

# How long a refresh's answer stays for the callers that asked at the same time, in seconds.
_REFRESH_ANSWER_SECONDS = 60

# How often a caller waiting for another's refresh checks for its answer, in seconds.
_REFRESH_POLL_INTERVAL = 0.2


class UpstreamAuthorizationCodeRefresher:
    """Refreshes an upstream token once per refresh token, however many callers ask at the same time.

    A rotated refresh token cannot be spent twice, and some upstreams revoke the whole family when one is.
    So the first caller claims the refresh in the shared store and leaves the upstream's answer there,
    and every other caller, in this gateway instance or another sharing the store, takes that answer.
    A refresh that fails releases the claim, so a caller still waiting tries for itself.
    """

    def __init__(self, upstream: UpstreamAuthorizationCodeClient, store: TokenStore) -> None:
        self._upstream = upstream
        self._store = store

    async def refresh(self, refresh_token: str) -> UpstreamTokenResponse:
        """The upstream's answer to ``refresh_token``, from this caller's request or another's.

        Raises ``HTTPException`` when the upstream refuses it, or when no answer comes in time.
        """
        key = hashlib.sha256(refresh_token.encode()).hexdigest()
        with anyio.move_on_after(2 * _REFRESH_CLAIM_SECONDS):
            while True:
                answer = await self._store.get(UPSTREAM_REFRESH_ANSWER_NAMESPACE, key)
                if answer is not None:
                    return UpstreamTokenResponse(**answer)
                if await self._store.set_if_absent(
                    UPSTREAM_REFRESH_CLAIM_NAMESPACE, key, True, ttl=_REFRESH_CLAIM_SECONDS
                ):
                    return await self._refresh_as_claimant(refresh_token, key)
                await anyio.sleep(_REFRESH_POLL_INTERVAL)
        logger.warning('Upstream token refresh timed out waiting for another caller')
        raise HTTPException(400, 'The upstream token refresh did not finish in time')

    async def _refresh_as_claimant(self, refresh_token: str, key: str) -> UpstreamTokenResponse:
        try:
            refreshed = await self._upstream.refresh(refresh_token)
        except BaseException:
            await self._store.delete(UPSTREAM_REFRESH_CLAIM_NAMESPACE, key)
            raise
        await self._store.set(
            UPSTREAM_REFRESH_ANSWER_NAMESPACE, key, dataclasses.asdict(refreshed), ttl=_REFRESH_ANSWER_SECONDS
        )
        return refreshed
