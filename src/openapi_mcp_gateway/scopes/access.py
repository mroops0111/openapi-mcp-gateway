import typing

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.uri_template import UriTemplate

from ..auth.flows.authorization_code import BrokeredAccessToken
from ..openapi import OperationInfo
from .policy import ScopePolicy
from .requirement import ScopeRequirement, Unrestricted
from .upstream import UnenforcedUpstreamScopes, UpstreamScopePolicy


class TokenGrant(typing.NamedTuple):
    """The scopes an MCP token carries, and the scopes the upstream granted the user behind it."""

    mcp_scopes: frozenset[str]
    upstream_scopes: frozenset[str]


class OperationAccess:
    """What each exposed name requires of a token, filled in as tools, operations and resources are registered.

    A name requires the MCP scopes ``policy`` gives its operation, and the upstream scopes ``upstream_policy`` does.
    A tool or operation is known by its name, and a resource by its URI or URI template.
    A concrete URI a template expands to requires what the template does.
    A name it never registered, such as a dynamic meta-tool, requires nothing,
    since the operations behind it are what the scopes guard.
    """

    def __init__(self, policy: ScopePolicy, upstream_policy: UpstreamScopePolicy | None = None) -> None:
        self.policy = policy
        self.upstream_policy = upstream_policy or UnenforcedUpstreamScopes()
        self._requirements: dict[str, _Requirements] = {}
        self._resource_templates: list[tuple[UriTemplate, _Requirements]] = []

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, private once either policy makes them depend on the token."""
        private = 'private' in (self.policy.list_cache_scope, self.upstream_policy.list_cache_scope)
        return 'private' if private else 'public'

    def register(self, name: str, operation: OperationInfo) -> None:
        """Record what ``operation``, exposed as the tool or operation ``name``, requires."""
        self._requirements[name] = _Requirements(
            self.policy.requirement_for(operation), self.upstream_policy.requirement_for(operation)
        )

    def register_resource(self, uri_template: str, operation: OperationInfo) -> None:
        """Record what ``operation``, exposed as a resource at ``uri_template``, requires."""
        self.register(uri_template, operation)
        if UriTemplate.is_template(uri_template):
            self._resource_templates.append((UriTemplate.parse(uri_template), self._requirements[uri_template]))

    def mcp_requirement(self, name: str) -> ScopeRequirement:
        """What a token's MCP scopes need to use ``name``, the part a client can step up for."""
        return self._requirements_of(name).mcp

    def refusal(self, name: str, grant: TokenGrant) -> str | None:
        """Why a token with ``grant`` may not use ``name``, or ``None`` when it may.

        The MCP scopes are checked first, since a client can step up for those and not for the upstream grant.
        """
        requirements = self._requirements_of(name)
        if not requirements.mcp.is_met(grant.mcp_scopes):
            return requirements.mcp.refusal
        if not requirements.upstream.is_met(self.upstream_policy.held_scopes(grant.upstream_scopes)):
            return requirements.upstream.refusal
        return None

    def permits(self, name: str, grant: TokenGrant) -> bool:
        """Whether a token with ``grant`` may list and use ``name``."""
        return self.refusal(name, grant) is None

    def _requirements_of(self, name: str) -> '_Requirements':
        if name in self._requirements:
            return self._requirements[name]
        return next(
            (requirements for template, requirements in self._resource_templates if template.match(name) is not None),
            _UNREGISTERED,
        )


def current_grant() -> TokenGrant:
    """The grant of the token on the request being handled, or an empty one outside an authenticated request."""
    access_token = get_access_token()
    if access_token is None:
        return TokenGrant(frozenset(), frozenset())
    upstream_scopes = access_token.upstream_scopes if isinstance(access_token, BrokeredAccessToken) else ()
    return TokenGrant(frozenset(access_token.scopes), frozenset(upstream_scopes))


class _Requirements(typing.NamedTuple):
    """What one name requires of the MCP scopes and of the upstream grant."""

    mcp: ScopeRequirement
    upstream: ScopeRequirement


# What a name nobody registered requires.
_UNREGISTERED = _Requirements(Unrestricted(), Unrestricted())
