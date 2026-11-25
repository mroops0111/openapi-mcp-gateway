import logging
import secrets
import urllib.parse

import anyio
import httpx
import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.exceptions import HTTPException

from ...scopes import ScopeRegistry
from ..mcp.consent import PENDING_AUTHORIZATION_TTL
from ..mcp.record_origin import McpRecordOrigin
from ..mcp.token_issuer import MCP_GRANT_NAMESPACE, McpTokenAttachment, McpTokenIssuer
from ..upstream.authorization_code import UpstreamAuthorizationCodeClient
from ..upstream.authorization_code_refresh import UpstreamAuthorizationCodeRefresher
from ..upstream.reauthorization import UpstreamReauthorizer, UpstreamSignInOffer
from ..upstream.rejection import UpstreamRejection
from .authorization_code_upstream_token import (
    UPSTREAM_REFRESH_TOKEN_LINK,
    build_renewed_attachment,
    build_upstream_links,
    compute_expiry,
)


logger = logging.getLogger(__name__)

# Where the user starts a new upstream sign-in for MCP tokens the client already holds, under the server's own path.
REAUTHORIZE_PATH = '/auth/reauthorize'

# Where such a sign-in waits, keyed by its id, and the ``state`` it sends to the upstream.
# A grant has at most one waiting at a time, which its record in ``MCP_GRANT_NAMESPACE`` maps to.
UPSTREAM_REAUTHORIZATION_NAMESPACE = 'upstream_reauthorization'
UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE = 'upstream_reauthorization_state'


# How often a call waiting for a new upstream sign-in checks whether it finished, in seconds.
_SIGN_IN_POLL_INTERVAL = 1.0

# What a browser is told about a sign-in that cannot run, whatever the reason.
_SIGN_IN_ENDED = 'This sign-in has expired or was already finished'


class AuthorizationCodePendingReauthorization(pydantic.BaseModel):
    """A new upstream sign-in for MCP tokens the client already holds.

    ``grant_id`` is the first sign-in behind those tokens, whose current tokens get the new upstream credential,
    however often the client refreshed meanwhile.
    ``browser_id`` is the browser the first sign-in ran in, the only one that may finish this one,
    which is how the spec's phishing guidance is met without the gateway knowing who the user is.
    ``scopes`` are what the sign-in asks the upstream for.
    ``completed`` turns true once the new upstream token is in place.
    """

    reauthorization_id: str
    grant_id: str
    browser_id: str
    scopes: list[str]
    origin: McpRecordOrigin
    completed: bool = False


class AuthorizationCodeReauthorizer(UpstreamReauthorizer):
    """Cures an upstream refusal of a user's own upstream token, for ``authorization_code``.

    An expired token is refreshed without the user.
    A token short of a scope gets a new upstream sign-in,
    which the user starts at ``reauthorize_url`` in the browser of the first sign-in,
    and which lands on whatever MCP tokens the client holds by then.

    ``tokens`` issues the MCP tokens whose upstream credentials it replaces.
    ``refresher`` refreshes an upstream token once however many calls ask, shared with the MCP refresh.
    ``scope_registry`` chooses what a first sign-in asks the upstream for, which a new one asks for too.
    """

    def __init__(
        self,
        tokens: McpTokenIssuer,
        upstream: UpstreamAuthorizationCodeClient,
        refresher: UpstreamAuthorizationCodeRefresher,
        scope_registry: ScopeRegistry,
        reauthorize_url: str,
    ) -> None:
        self.tokens = tokens
        self.upstream = upstream
        self._refresher = refresher
        self._scope_registry = scope_registry
        self._reauthorize_url = reauthorize_url

    async def renew_token(self) -> bool:
        """Refresh the upstream token behind the call's MCP token with the refresh token the upstream issued.

        Calls refused together, in any gateway instance, share one refresh through ``refresher``.
        """
        attachment = await self._load_caller_attachment()
        upstream_refresh_token = attachment.links.get(UPSTREAM_REFRESH_TOKEN_LINK) if attachment else None
        if attachment is None or attachment.grant_id is None or not upstream_refresh_token:
            return False
        try:
            refreshed = await self._refresher.refresh(upstream_refresh_token)
        except (HTTPException, httpx.HTTPError, ValueError) as exception:
            # A token endpoint that is down or answers garbage leaves the upstream's refusal as the answer.
            logger.info('Upstream token renewal failed: error=%s', exception)
            return False
        renewed = build_renewed_attachment(attachment, refreshed, upstream_refresh_token)
        return await self.tokens.replace_grant_attachment(attachment.grant_id, renewed)

    async def offer_sign_in(self, rejection: UpstreamRejection) -> UpstreamSignInOffer | None:
        """Record a new upstream sign-in for the call's MCP token, and return the page that starts it.

        It asks for what a first sign-in with the token's MCP scopes would, what the upstream granted so far,
        and what the refusal names, so a new sign-in never narrows the grant.
        Calls refused while one is waiting share it, so the user signs in once for all of them,
        and it asks for what each of them needs.
        """
        mcp_access_token = get_access_token()
        attachment = await self._load_caller_attachment()
        if mcp_access_token is None or attachment is None or not attachment.browser_id or not attachment.grant_id:
            return None
        if not self._may_ask_for(rejection.scopes):
            logger.info('Upstream sign-in not offered: reason=beyond_scopes scopes=%s', list(rejection.scopes))
            return None
        pending = await self._load_pending(await self._load_pending_id(attachment.grant_id))
        scopes = [
            *(pending.scopes if pending else []),
            *self._scope_registry.requested_upstream_scopes(
                self.upstream.scopes, self.upstream.required_scopes, mcp_access_token.scopes
            ),
            *attachment.upstream_scopes,
            *rejection.scopes,
        ]
        pending_reauthorization = AuthorizationCodePendingReauthorization(
            reauthorization_id=pending.reauthorization_id if pending else secrets.token_urlsafe(32),
            grant_id=attachment.grant_id,
            browser_id=attachment.browser_id,
            scopes=list(dict.fromkeys(scopes)),
            origin=self.tokens.origin,
        )
        await self._save_pending(pending_reauthorization)
        await self.tokens.store.set_mapping(
            MCP_GRANT_NAMESPACE,
            attachment.grant_id,
            UPSTREAM_REAUTHORIZATION_NAMESPACE,
            pending_reauthorization.reauthorization_id,
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        logger.info('Upstream sign-in offered: reason=%s scopes=%s', rejection.error, pending_reauthorization.scopes)
        query = urllib.parse.urlencode({'id': pending_reauthorization.reauthorization_id})
        return UpstreamSignInOffer(
            reauthorization_id=pending_reauthorization.reauthorization_id,
            url=f'{self._reauthorize_url}?{query}',
            message=f'Sign in to {self.upstream.host} again so this call can go on.',
        )

    async def wait_for_sign_in(self, timeout: float) -> bool:
        attachment = await self._load_caller_attachment()
        if attachment is None or attachment.grant_id is None:
            return False
        reauthorization_id = await self._load_pending_id(attachment.grant_id)
        if reauthorization_id is None:
            return False
        with anyio.move_on_after(timeout):
            while True:
                data = await self.tokens.store.get(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
                if not self.tokens.is_from_here(data):
                    return False
                if data.get('completed'):
                    return True
                await anyio.sleep(_SIGN_IN_POLL_INTERVAL)
        return False

    # The page that starts a new upstream sign-in, and the upstream's redirect back from it

    async def begin_sign_in(self, reauthorization_id: str, browser_id: str | None) -> str:
        """Send the browser to the upstream for the sign-in ``reauthorization_id`` names, under a fresh ``state``."""
        pending_reauthorization = await self._load_pending_in_browser(reauthorization_id, browser_id)
        state = secrets.token_urlsafe(32)
        await self.tokens.store.set(
            UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE,
            state,
            {'reauthorization_id': reauthorization_id},
            ttl=PENDING_AUTHORIZATION_TTL,
        )
        return self.upstream.build_authorize_url(state, pending_reauthorization.scopes)

    async def is_sign_in_state(self, state: str) -> bool:
        """Whether the upstream's callback under ``state`` finishes one of these sign-ins."""
        return await self.tokens.store.get(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state) is not None

    async def handle_upstream_callback(
        self, code: str, state: str, browser_id: str | None, iss: str | None = None
    ) -> None:
        """Redeem the upstream ``code`` and put the new upstream token behind the current tokens of the grant."""
        reauthorization_id = await self._load_pending_id_under_state(state)
        pending_reauthorization = await self._load_pending_in_browser(reauthorization_id, browser_id)
        self.upstream.validate_issuer(iss)

        upstream_token = await self.upstream.exchange_code(code)
        # Some upstreams, Google among them, send a refresh token on first consent only, so the old one is kept.
        upstream_refresh_token = upstream_token.refresh_token
        if not upstream_refresh_token:
            current = await self.tokens.load_grant_attachment(pending_reauthorization.grant_id)
            upstream_refresh_token = current.links.get(UPSTREAM_REFRESH_TOKEN_LINK) if current else None
        attachment = McpTokenAttachment(
            upstream_scopes=upstream_token.resolve_granted_scopes(pending_reauthorization.scopes),
            links=build_upstream_links(upstream_token.access_token, upstream_refresh_token),
            links_expire_at=compute_expiry(upstream_token.expires_in),
            browser_id=pending_reauthorization.browser_id,
            grant_id=pending_reauthorization.grant_id,
        )
        await self.tokens.store.delete(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        if not await self.tokens.replace_grant_attachment(pending_reauthorization.grant_id, attachment):
            await self.tokens.store.delete(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
            raise HTTPException(400, 'The MCP sign-in this was for has ended')
        await self._save_pending(pending_reauthorization.model_copy(update={'completed': True}))
        logger.info('Upstream signed in again: expires_in=%s', upstream_token.expires_in)

    async def handle_upstream_error(self, error: str, state: str, browser_id: str | None) -> None:
        """End a sign-in the upstream refused, so the call waiting on it stops waiting."""
        reauthorization_id = await self._load_pending_id_under_state(state)
        await self._load_pending_in_browser(reauthorization_id, browser_id)
        await self.tokens.store.delete(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        await self.tokens.store.delete(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
        logger.info('Upstream OAuth refused the new sign-in: error=%s', error)
        raise HTTPException(400, f'The sign-in service answered {error}')

    async def _load_caller_attachment(self) -> McpTokenAttachment | None:
        """What the MCP access token of the call being handled carries, or ``None`` outside a call."""
        mcp_access_token = get_access_token()
        if mcp_access_token is None:
            return None
        return await self.tokens.load_access_token_attachment(mcp_access_token.token)

    def _may_ask_for(self, scopes: tuple[str, ...]) -> bool:
        """Whether a sign-in may ask the upstream for ``scopes``, which ``auth.upstream.scopes`` caps when set."""
        if not scopes or not self.upstream.scopes:
            return True
        ceiling = [*self.upstream.required_scopes, *self.upstream.scopes]
        return set(scopes) <= self._scope_registry.upstream_policy.held_scopes(ceiling)

    async def _load_pending_id(self, grant_id: str) -> str | None:
        return await self.tokens.store.get_mapping(MCP_GRANT_NAMESPACE, grant_id, UPSTREAM_REAUTHORIZATION_NAMESPACE)

    async def _load_pending_id_under_state(self, state: str) -> str:
        data = await self.tokens.store.get(UPSTREAM_REAUTHORIZATION_STATE_NAMESPACE, state)
        if not data:
            raise HTTPException(400, _SIGN_IN_ENDED)
        return data['reauthorization_id']

    async def _load_pending(self, reauthorization_id: str | None) -> AuthorizationCodePendingReauthorization | None:
        """The sign-in ``reauthorization_id`` names while it still waits, or ``None`` once it ended or finished.

        A record of another shape, such as one an older gateway stored, reads as ended.
        """
        data = (
            await self.tokens.store.get(UPSTREAM_REAUTHORIZATION_NAMESPACE, reauthorization_id)
            if reauthorization_id
            else None
        )
        if not self.tokens.is_from_here(data) or data.get('completed'):
            return None
        try:
            return AuthorizationCodePendingReauthorization.model_validate(data)
        except pydantic.ValidationError:
            return None

    async def _load_pending_in_browser(
        self, reauthorization_id: str, browser_id: str | None
    ) -> AuthorizationCodePendingReauthorization:
        """The sign-in ``reauthorization_id`` names, which only the browser of the first sign-in may run.

        Raises ``HTTPException`` when it ended, finished, or was opened in another browser.
        """
        pending_reauthorization = await self._load_pending(reauthorization_id)
        if pending_reauthorization is None:
            logger.warning('Upstream sign-in rejected: reason=invalid_reauthorization')
            raise HTTPException(400, _SIGN_IN_ENDED)
        if browser_id is None or pending_reauthorization.browser_id != browser_id:
            logger.warning('Upstream sign-in rejected: reason=other_browser')
            raise HTTPException(
                403, 'Open this link in the browser you first signed in with, so the gateway knows it is you'
            )
        return pending_reauthorization

    async def _save_pending(self, pending_reauthorization: AuthorizationCodePendingReauthorization) -> None:
        await self.tokens.store.set(
            UPSTREAM_REAUTHORIZATION_NAMESPACE,
            pending_reauthorization.reauthorization_id,
            pending_reauthorization.model_dump(),
            ttl=PENDING_AUTHORIZATION_TTL,
        )
