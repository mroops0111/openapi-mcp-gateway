import typing

from mcp.shared.uri_template import UriTemplate

from ..openapi import OperationInfo
from .grant import TokenGrant
from .policy import McpScopePolicy, UnrestrictedUpstreamScopePolicy, UpstreamScopePolicy
from .requirement import ScopeRequirement, UnrestrictedRequirement


class ScopeRegistry:
    """What each exposed name requires of a token, filled in as tools, operations and resources are registered.

    A name requires the MCP scopes ``mcp_policy`` gives its operation, and the upstream scopes ``upstream_policy`` does.
    A tool or operation is known by its name, and a resource by its URI or URI template.
    A concrete URI a template expands to requires what the template does.
    A name it never registered, such as a dynamic meta-tool, requires nothing,
    since the operations behind it are what the scopes guard.
    """

    def __init__(self, mcp_policy: McpScopePolicy, upstream_policy: UpstreamScopePolicy | None = None) -> None:
        self.mcp_policy = mcp_policy
        self.upstream_policy = upstream_policy or UnrestrictedUpstreamScopePolicy()
        self._requirements: dict[str, _NameRequirements] = {}
        self._resource_templates: list[tuple[UriTemplate, _NameRequirements]] = []

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, private once either policy makes them depend on the token."""
        private = 'private' in (self.mcp_policy.list_cache_scope, self.upstream_policy.list_cache_scope)
        return 'private' if private else 'public'

    def register(self, name: str, operation: OperationInfo) -> None:
        """Record what ``operation``, exposed as the tool or operation ``name``, requires."""
        self._requirements[name] = _NameRequirements(
            self.mcp_policy.requirement_for(operation), self.upstream_policy.requirement_for(operation)
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

    def _requirements_of(self, name: str) -> '_NameRequirements':
        if name in self._requirements:
            return self._requirements[name]
        return next(
            (requirements for template, requirements in self._resource_templates if template.match(name) is not None),
            _UNREGISTERED,
        )


class _NameRequirements(typing.NamedTuple):
    """What one name requires of the MCP scopes and of the upstream grant."""

    mcp: ScopeRequirement
    upstream: ScopeRequirement


# What a name nobody registered requires.
_UNREGISTERED = _NameRequirements(UnrestrictedRequirement(), UnrestrictedRequirement())
