import typing

import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken


class BrokeredAccessToken(AccessToken):
    """An MCP access token the gateway minted, carrying the scopes the upstream granted the user behind it."""

    upstream_scopes: list[str] = pydantic.Field(default_factory=list)


class TokenGrant(typing.NamedTuple):
    """The scopes an MCP token carries, and the scopes the upstream granted the user behind it."""

    mcp_scopes: frozenset[str]
    upstream_scopes: frozenset[str]


def current_grant() -> TokenGrant:
    """The grant of the token on the request being handled, or an empty one outside an authenticated request."""
    access_token = get_access_token()
    if access_token is None:
        return TokenGrant(frozenset(), frozenset())
    upstream_scopes = access_token.upstream_scopes if isinstance(access_token, BrokeredAccessToken) else ()
    return TokenGrant(frozenset(access_token.scopes), frozenset(upstream_scopes))
