import time

from ..mcp.token_issuer import McpTokenAttachment
from ..upstream.authorization_code import UpstreamTokenResponse


# The names under which each MCP token links the upstream tokens behind it.
UPSTREAM_ACCESS_TOKEN_LINK = 'upstream_access_token'  # noqa: S105
UPSTREAM_REFRESH_TOKEN_LINK = 'upstream_refresh_token'  # noqa: S105


def build_upstream_links(access_token: str, refresh_token: str | None) -> dict[str, str]:
    """The links an MCP token carries to the upstream tokens behind it."""
    links = {UPSTREAM_ACCESS_TOKEN_LINK: access_token}
    if refresh_token:
        links[UPSTREAM_REFRESH_TOKEN_LINK] = refresh_token
    return links


def compute_expiry(expires_in: int | None) -> int | None:
    """When a token lasting ``expires_in`` seconds from now stops working, or ``None`` for one that never does."""
    return None if expires_in is None else int(time.time()) + expires_in


def build_renewed_attachment(
    attachment: McpTokenAttachment, refreshed: UpstreamTokenResponse, upstream_refresh_token: str
) -> McpTokenAttachment:
    """``attachment`` after the upstream refreshed its token, keeping the refresh token when no new one came."""
    return McpTokenAttachment(
        upstream_scopes=refreshed.resolve_granted_scopes(attachment.upstream_scopes),
        links=build_upstream_links(refreshed.access_token, refreshed.refresh_token or upstream_refresh_token),
        links_expire_at=compute_expiry(refreshed.expires_in),
        browser_id=attachment.browser_id,
        grant_id=attachment.grant_id,
    )
