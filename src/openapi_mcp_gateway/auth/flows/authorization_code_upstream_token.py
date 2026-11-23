import dataclasses
import hashlib
import logging
import time

import anyio
from starlette.exceptions import HTTPException

from ...stores.base import TokenStore
from ..mcp.token_issuer import McpTokenAttachment
from ..upstream.authorization_code import UpstreamAuthorizationCodeClient, UpstreamTokenResponse


logger = logging.getLogger(__name__)

# The names under which each MCP token links the upstream tokens behind it.
UPSTREAM_ACCESS_TOKEN_LINK = 'upstream_access_token'  # noqa: S105
UPSTREAM_REFRESH_TOKEN_LINK = 'upstream_refresh_token'  # noqa: S105

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


class UpstreamTokenRefresher:
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


def build_upstream_links(access_token: str, refresh_token: str | None) -> dict[str, str]:
    """The links an MCP token carries to the upstream tokens behind it."""
    links = {UPSTREAM_ACCESS_TOKEN_LINK: access_token}
    if refresh_token:
        links[UPSTREAM_REFRESH_TOKEN_LINK] = refresh_token
    return links


def expiry_of(expires_in: int | None) -> int | None:
    """When a token lasting ``expires_in`` seconds from now stops working, or ``None`` for one that never does."""
    return None if expires_in is None else int(time.time()) + expires_in


def renewed_attachment(
    attachment: McpTokenAttachment, refreshed: UpstreamTokenResponse, upstream_refresh_token: str
) -> McpTokenAttachment:
    """``attachment`` after the upstream refreshed its token, keeping the refresh token when no new one came."""
    return McpTokenAttachment(
        upstream_scopes=refreshed.resolve_granted_scopes(attachment.upstream_scopes),
        links=build_upstream_links(refreshed.access_token, refreshed.refresh_token or upstream_refresh_token),
        links_expire_at=expiry_of(refreshed.expires_in),
        browser_id=attachment.browser_id,
        grant_id=attachment.grant_id,
    )
