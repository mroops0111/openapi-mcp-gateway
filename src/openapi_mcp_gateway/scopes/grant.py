import typing

import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken


class BrokeredAccessToken(AccessToken):
    """An MCP access token the gateway minted, carrying the scopes the upstream granted the user behind it.

    ``grantable_mcp_scopes`` is every MCP scope its client may be granted, or ``None`` on a token minted before it was recorded.
    """

    upstream_scopes: list[str] = pydantic.Field(default_factory=list)
    grantable_mcp_scopes: list[str] | None = None


class TokenGrant(typing.NamedTuple):
    """The scopes an MCP token carries, the scopes the upstream granted the user behind it,
    and every MCP scope its client may be granted, which is what it can step up to.
    """

    mcp_scopes: frozenset[str]
    upstream_scopes: frozenset[str]
    grantable_mcp_scopes: frozenset[str]


def current_grant() -> TokenGrant:
    """The grant of the token on the request being handled, or an empty one outside an authenticated request."""
    return grant_of(get_access_token())


def grant_of(access_token: AccessToken | None) -> TokenGrant:
    """The grant ``access_token`` carries, or an empty one for no token."""
    if access_token is None:
        return TokenGrant(frozenset(), frozenset(), frozenset())
    mcp_scopes = frozenset(access_token.scopes)
    if not isinstance(access_token, BrokeredAccessToken):
        return TokenGrant(mcp_scopes, frozenset(), mcp_scopes)
    # A token minted before the client's scopes were recorded can step up to nothing it does not already hold.
    grantable = access_token.grantable_mcp_scopes
    return TokenGrant(
        mcp_scopes, frozenset(access_token.upstream_scopes), mcp_scopes if grantable is None else frozenset(grantable)
    )
