import collections.abc
import typing

from mcp.shared.uri_template import UriTemplate

from ..openapi import OperationInfo
from .grant import TokenGrant, UpstreamGrantSource, UpstreamGrantUnavailableError, current_grant
from .policy import McpScopePolicy, UpstreamScopePolicy, UpstreamUnrestrictedScopePolicy
from .requirement import ScopeRequirement, UnrestrictedRequirement


class ScopeRegistry:
    """What each exposed name requires of a token, filled in as tools, operations and resources are registered.

    A name requires the MCP scopes ``mcp_policy`` gives its operation, and the upstream scopes ``upstream_policy`` does.
    A tool or operation is known by its name, and a resource by its URI or URI template.
    A concrete URI a template expands to requires what the template does.
    A name it never registered, such as a dynamic meta-tool, requires nothing,
    since the operations behind it are what the scopes guard.
    The operations it registered are also what a sign-in asks the upstream for, as ``requested_upstream_scopes`` says.
    """

    def __init__(self, mcp_policy: McpScopePolicy, upstream_policy: UpstreamScopePolicy | None = None) -> None:
        self.mcp_policy = mcp_policy
        self.upstream_policy = upstream_policy or UpstreamUnrestrictedScopePolicy()
        self._requirements: dict[str, _NameRequirements] = {}
        self._operations: dict[str, _RegisteredOperation] = {}
        self._requested_scopes: dict[tuple[tuple[str, ...], tuple[str, ...], frozenset[str]], list[str]] = {}
        self._resource_templates: list[tuple[UriTemplate, _NameRequirements]] = []

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, private once either policy makes them depend on the token."""
        private = 'private' in (self.mcp_policy.list_cache_scope, self.upstream_policy.list_cache_scope)
        return 'private' if private else 'public'

    def register(self, name: str, operation: OperationInfo) -> None:
        """Record what ``operation``, exposed as the tool or operation ``name``, requires."""
        requirements = _NameRequirements(
            self.mcp_policy.requirement_for(operation), self.upstream_policy.requirement_for(operation)
        )
        self._requirements[name] = requirements
        self._operations[operation.operation_id] = _RegisteredOperation(operation, requirements.mcp)
        self._requested_scopes.clear()

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

        The MCP scopes are checked first, since a client steps up for those,
        and the sign-in a step-up starts is what asks the upstream for more.
        An upstream grant not yet found out is not checked, as ``TokenGrant`` says.
        """
        requirements = self._requirements_of(name)
        if not requirements.mcp.is_met(grant.mcp_scopes):
            return requirements.mcp.refusal
        if not self._meets_upstream(requirements.upstream, grant):
            return requirements.upstream.refusal
        return None

    async def call_refusal(self, name: str, grant_source: UpstreamGrantSource) -> str | None:
        """Why the call being handled may not use ``name``, or ``None`` when it may.

        ``grant_source`` is asked for the upstream grant only once the MCP scopes are met,
        and only where ``name`` needs an upstream grant at all, since finding it out may mean an exchange.
        A grant that cannot be found out refuses the call with the reason.
        """
        requirements = self._requirements_of(name)
        grant = current_grant()
        if not requirements.mcp.is_met(grant.mcp_scopes):
            return requirements.mcp.refusal
        if requirements.upstream.is_met(frozenset()):
            return None
        try:
            grant = await grant_source.read_call_grant()
        except UpstreamGrantUnavailableError as error:
            return str(error)
        return self.refusal(name, grant)

    def lists(self, name: str, grant: TokenGrant) -> bool:
        """Whether ``name`` shows in the lists and searches a token with ``grant`` is answered with.

        The MCP scopes are checked against those the token can step up to rather than those it holds,
        so a tool the client can step up for stays in sight, and calling it starts the step-up.
        While the MCP scopes it needs are not yet held, the upstream grant is not checked,
        since the sign-in a step-up starts asks the upstream for what they need.
        Once they are held, stepping up cannot change the upstream grant, so it is checked as it stands.
        An upstream grant not yet found out leaves the name in sight, as the call checks it.
        """
        requirements = self._requirements_of(name)
        if not requirements.mcp.is_met(grant.mcp_reachable_scopes):
            return False
        if not requirements.mcp.is_met(grant.mcp_scopes):
            return True
        return self._meets_upstream(requirements.upstream, grant)

    def requested_upstream_scopes(
        self,
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
        mcp_scopes: collections.abc.Iterable[str],
    ) -> list[str]:
        """The upstream scopes a sign-in granting ``mcp_scopes`` asks for.

        The sign-in opens the registered operations whose MCP requirement ``mcp_scopes`` meets.
        ``upstream_policy`` asks for ``required_scopes`` and picks what of ``ceiling`` those operations need.
        Each answer is kept until another operation is registered, since every sign-in asks the same question again.
        Only the scopes ``mcp_policy`` defines can open an operation, so only they tell one answer from another.
        """
        granted_scopes = frozenset(mcp_scopes).intersection(self.mcp_policy.mcp_defined_scopes)
        key = (tuple(ceiling), tuple(required_scopes), granted_scopes)
        if key not in self._requested_scopes:
            opened_operations = [
                registered.operation
                for registered in self._operations.values()
                if registered.mcp_requirement.is_met(granted_scopes)
            ]
            self._requested_scopes[key] = self.upstream_policy.requested_scopes(
                ceiling, required_scopes, opened_operations
            )
        return list(self._requested_scopes[key])

    def _meets_upstream(self, requirement: ScopeRequirement, grant: TokenGrant) -> bool:
        if grant.upstream_scopes is None:
            return True
        return requirement.is_met(self.upstream_policy.held_scopes(grant.upstream_scopes))

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


class _RegisteredOperation(typing.NamedTuple):
    """A registered operation, with what it requires of the MCP scopes."""

    operation: OperationInfo
    mcp_requirement: ScopeRequirement


# What a name nobody registered requires.
_UNREGISTERED = _NameRequirements(UnrestrictedRequirement(), UnrestrictedRequirement())
