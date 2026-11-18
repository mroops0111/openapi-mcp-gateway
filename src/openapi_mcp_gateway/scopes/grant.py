import typing

import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken


class McpAccessToken(AccessToken):
    """An MCP access token the gateway minted, carrying the scopes the upstream granted the user behind it.

    ``mcp_grantable_scopes`` is every MCP scope its client may be granted.
    It is ``None`` on a token minted before it was recorded.
    """

    upstream_scopes: list[str] = pydantic.Field(default_factory=list)
    mcp_grantable_scopes: list[str] | None = None


class TokenGrant(typing.NamedTuple):
    """What an MCP token holds and what it can step up to.

    ``mcp_scopes`` are the scopes it carries, and ``upstream_scopes`` what the upstream granted the user behind it.
    ``mcp_grantable_scopes`` are every MCP scope its client may be granted, which is what it can step up to.
    They are ``None`` on a token minted before they were recorded.
    """

    mcp_scopes: frozenset[str]
    upstream_scopes: frozenset[str]
    mcp_grantable_scopes: frozenset[str] | None

    @property
    def mcp_reachable_scopes(self) -> frozenset[str]:
        """The MCP scopes the token holds or can step up to, which are only those it holds when that is unknown."""
        return self.mcp_scopes if self.mcp_grantable_scopes is None else self.mcp_grantable_scopes


def current_grant() -> TokenGrant:
    """The grant of the token on the request being handled, or an empty one outside an authenticated request."""
    return grant_of(get_access_token())


def grant_of(access_token: AccessToken | None) -> TokenGrant:
    """The grant ``access_token`` carries, or an empty one for no token."""
    if access_token is None:
        return TokenGrant(frozenset(), frozenset(), frozenset())
    mcp_scopes = frozenset(access_token.scopes)
    if not isinstance(access_token, McpAccessToken):
        return TokenGrant(mcp_scopes, frozenset(), mcp_scopes)
    grantable = access_token.mcp_grantable_scopes
    return TokenGrant(
        mcp_scopes, frozenset(access_token.upstream_scopes), None if grantable is None else frozenset(grantable)
    )
