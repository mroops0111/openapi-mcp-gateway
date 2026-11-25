import abc
import typing

import pydantic
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken


class McpAccessToken(AccessToken):
    """An MCP access token as the gateway accepted it, whoever minted it.

    ``upstream_scopes`` are what the upstream granted the user behind it,
    or ``None`` where the token records no upstream grant and the call finds it out.
    ``mcp_grantable_scopes`` is every MCP scope its client may be granted.
    It is ``None`` on a token minted before it was recorded.
    """

    upstream_scopes: list[str] | None = pydantic.Field(default_factory=list)
    mcp_grantable_scopes: list[str] | None = None


class TokenGrant(typing.NamedTuple):
    """What an MCP token holds and what it can step up to.

    ``mcp_scopes`` are the scopes it carries, and ``upstream_scopes`` what the upstream granted the user behind it.
    ``upstream_scopes`` are ``None`` until the call finds them out, where the token records none.
    ``mcp_grantable_scopes`` are every MCP scope its client may be granted, which is what it can step up to.
    They are ``None`` on a token minted before they were recorded.
    """

    mcp_scopes: frozenset[str]
    upstream_scopes: frozenset[str] | None
    mcp_grantable_scopes: frozenset[str] | None

    @property
    def mcp_reachable_scopes(self) -> frozenset[str]:
        """The MCP scopes the token holds or can step up to, which are only those it holds when that is unknown."""
        return self.mcp_scopes if self.mcp_grantable_scopes is None else self.mcp_grantable_scopes


class UpstreamGrantUnavailableError(Exception):
    """The upstream grant of a call could not be found out, so the call is refused with this message."""


class UpstreamGrantSource(abc.ABC):
    """Where the upstream grant a tool call or resource read is checked against comes from.

    ``ScopeFilterMiddleware`` and ``call_operation`` ask it before the call goes upstream,
    so a flow that finds the grant out only then can supply it.
    """

    @property
    @abc.abstractmethod
    def records_user_grant(self) -> bool:
        """Whether each user's own upstream grant is known, which checking an operation's upstream scopes needs."""

    async def read_call_grant(self) -> TokenGrant:
        """The grant of the call being handled, as its token carries it, by default.

        Raises ``UpstreamGrantUnavailableError`` when the grant cannot be found out, such as a refused exchange.
        """
        return read_current_grant()


class UpstreamNoGrantSource(UpstreamGrantSource):
    """No user's own upstream grant, as for a shared credential or no upstream credential at all."""

    @property
    def records_user_grant(self) -> bool:
        return False


class UpstreamRecordedGrantSource(UpstreamGrantSource):
    """The upstream grant each MCP token recorded when its user signed in."""

    @property
    def records_user_grant(self) -> bool:
        return True


def read_current_grant() -> TokenGrant:
    """The grant of the token on the request being handled, or an empty one outside an authenticated request."""
    return read_grant(get_access_token())


def read_grant(access_token: AccessToken | None) -> TokenGrant:
    """The grant ``access_token`` carries, or an empty one for no token.

    Every verifier the gateway installs answers with an ``McpAccessToken``, so any other token is a wiring mistake.
    """
    if access_token is None:
        return TokenGrant(frozenset(), frozenset(), frozenset())
    if not isinstance(access_token, McpAccessToken):
        raise TypeError(f'Expected an McpAccessToken from the token verifier, got {type(access_token).__name__}')
    upstream = access_token.upstream_scopes
    grantable = access_token.mcp_grantable_scopes
    return TokenGrant(
        frozenset(access_token.scopes),
        None if upstream is None else frozenset(upstream),
        None if grantable is None else frozenset(grantable),
    )
