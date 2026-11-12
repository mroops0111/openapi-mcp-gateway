import abc
import collections.abc
import logging
import typing

from ..openapi import OperationInfo
from .policy import ScopePolicy
from .requirement import ScopeRequirement, Unrestricted


logger = logging.getLogger(__name__)


# The securitySchemes types whose requirements name OAuth scopes, the only credential the gateway holds upstream.
_OAUTH_SCHEME_TYPES = frozenset({'oauth2', 'openIdConnect'})

# How many operations a startup warning lists before it only counts the rest.
_LOGGED_OPERATION_LIMIT = 5


class AllScopesOfAnySet(ScopeRequirement):
    """Met by a grant holding every scope of any one set, as an operation's OpenAPI ``security`` reads.

    Each set is one security requirement the gateway's upstream OAuth token could meet,
    so the scopes inside a set are all needed, and any one set is enough.
    No set at all means no requirement of the operation names a credential the gateway holds.
    The upstream grant cannot change by stepping up at the gateway, so it is never challenged.
    """

    def __init__(self, scope_sets: collections.abc.Iterable[collections.abc.Iterable[str]], operation_id: str) -> None:
        self.scope_sets = tuple(dict.fromkeys(frozenset(scope_set) for scope_set in scope_sets))
        self._operation_id = operation_id

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return any(scope_set <= granted for scope_set in self.scope_sets)

    def missing(self, granted: collections.abc.Set[str]) -> frozenset[str]:
        """The fewest scopes ``granted`` lacks to meet one set, or none when no set can be met with scopes alone."""
        return min((scope_set - granted for scope_set in self.scope_sets), key=len, default=frozenset())

    @property
    def challenge(self) -> tuple[str, ...]:
        return ()

    @property
    def refusal(self) -> str:
        override = f'operations.{self._operation_id}.security'
        if not self.scope_sets:
            return (
                f'No security requirement of {self._operation_id} can be met with the upstream OAuth token. '
                f'Correct its security under {override} if the spec is wrong.'
            )
        needed = ' or '.join(' '.join(sorted(scope_set)) for scope_set in self.scope_sets)
        return (
            f'The upstream token lacks the scopes {self._operation_id} needs ({needed}). '
            f'Add them to auth.upstream.scopes and sign in again, or correct its security under {override}.'
        )


class UpstreamScopePolicy(abc.ABC):
    """What each operation requires of the scopes the upstream granted the user behind a token."""

    @property
    @abc.abstractmethod
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, which is private once they depend on the upstream grant."""

    @abc.abstractmethod
    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        """What the upstream grant must hold for ``operation`` to be listed and called."""

    def held_scopes(self, granted: collections.abc.Iterable[str]) -> frozenset[str]:
        """The scopes an upstream grant of ``granted`` holds, which are exactly those unless some imply others."""
        return frozenset(granted)


class UnenforcedUpstreamScopes(UpstreamScopePolicy):
    """Every operation open whatever the upstream granted, as on a server without ``auth.upstream.enforce_scopes``."""

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'public'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return Unrestricted()


class SpecSecurityScopes(UpstreamScopePolicy):
    """The upstream scopes each operation's OpenAPI ``security`` declares, checked against the gateway's credential.

    The gateway holds one upstream credential, an OAuth token,
    so a requirement is met only through the schemes whose ``type`` is ``oauth2`` or ``openIdConnect``.
    A requirement naming any other scheme, such as HTTP basic, cannot be met,
    while an empty requirement or an empty ``security`` needs nothing.
    A spec declaring no OAuth scheme describes some other credential, so it restricts nothing.
    ``scope_implies`` maps an upstream scope to the narrower scopes it covers, such as GitHub's ``repo``.
    """

    def __init__(
        self,
        security_schemes: collections.abc.Mapping[str, typing.Any],
        scope_implies: collections.abc.Mapping[str, collections.abc.Sequence[str]],
    ) -> None:
        self.oauth_schemes = frozenset(
            name
            for name, scheme in security_schemes.items()
            if isinstance(scheme, dict) and scheme.get('type') in _OAUTH_SCHEME_TYPES
        )
        self._scope_implies = scope_implies

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'private'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return self._scope_sets_of(operation) or Unrestricted()

    def held_scopes(self, granted: collections.abc.Iterable[str]) -> frozenset[str]:
        held = set(granted)
        pending = list(held)
        while pending:
            for implied in self._scope_implies.get(pending.pop(), ()):
                if implied not in held:
                    held.add(implied)
                    pending.append(implied)
        return frozenset(held)

    def requested_scopes(self, operations: collections.abc.Iterable[OperationInfo]) -> list[str]:
        """Every upstream scope any of ``operations`` names, in the order the operations name them."""
        return list(
            dict.fromkeys(
                scope
                for operation in operations
                if (requirement := self._scope_sets_of(operation))
                for scope_set in requirement.scope_sets
                for scope in sorted(scope_set)
            )
        )

    def warn_on_unrequested_scopes(
        self,
        operations: collections.abc.Sequence[OperationInfo],
        requested: collections.abc.Iterable[str],
        mcp_policy: ScopePolicy,
        client_scopes: collections.abc.Mapping[str, collections.abc.Sequence[str]],
        server_name: str,
    ) -> None:
        """Warn about the clients that can reach operations the requested upstream scopes cannot open.

        A client reaches the operations its MCP scopes open, and clients reaching the same ones share a warning.
        Without pre-registered clients, every operation counts, for whoever signs in.
        It warns rather than refuses, since an upstream scope hierarchy or an imprecise spec can make it wrong.
        """
        held = self.held_scopes(requested)
        unmet = {
            operation.operation_id: requirement.missing(held)
            for operation in operations
            if (requirement := self._scope_sets_of(operation)) and not requirement.is_met(held)
        }
        if not unmet:
            return
        audiences: dict[tuple[str, ...], list[str]] = {}
        for client_id, scopes in client_scopes.items():
            reachable = tuple(
                operation.operation_id
                for operation in operations
                if operation.operation_id in unmet and mcp_policy.requirement_for(operation).is_met(frozenset(scopes))
            )
            if reachable:
                audiences.setdefault(reachable, []).append(f'"{client_id}"')
        if not client_scopes:
            audiences[tuple(unmet)] = ['every client']
        for reachable, clients in audiences.items():
            described = [_describe_unmet(operation_id, unmet[operation_id]) for operation_id in reachable]
            logger.warning(
                'Server "%s": %s can reach %d operation(s) the requested upstream scopes cannot open (%s). '
                'Add the scopes to auth.upstream.scopes, or correct an operation under operations.<id>.security.',
                server_name,
                ', '.join(clients),
                len(described),
                ', '.join(described[:_LOGGED_OPERATION_LIMIT])
                + ('...' if len(described) > _LOGGED_OPERATION_LIMIT else ''),
            )

    def _scope_sets_of(self, operation: OperationInfo) -> AllScopesOfAnySet | None:
        """The scope sets ``operation`` requires, or ``None`` when it needs no upstream scope."""
        if not self.oauth_schemes or not operation.security:
            return None
        scope_sets = [
            {scope for scopes in requirement.values() for scope in scopes}
            for requirement in operation.security
            if requirement.keys() <= self.oauth_schemes
        ]
        if any(not scope_set for scope_set in scope_sets):
            return None
        return AllScopesOfAnySet(scope_sets, operation.operation_id)


def build_upstream_scope_policy(
    enforce_scopes: bool, spec_security: SpecSecurityScopes, server_name: str
) -> UpstreamScopePolicy:
    """Return the upstream scope policy for a server, which restricts nothing unless ``enforce_scopes`` is on."""
    if not enforce_scopes:
        return UnenforcedUpstreamScopes()
    if not spec_security.oauth_schemes:
        logger.warning(
            'Server "%s": auth.upstream.enforce_scopes is on, but the spec declares no OAuth security scheme, '
            'so no operation is restricted by the upstream grant.',
            server_name,
        )
    return spec_security


def _describe_unmet(operation_id: str, missing: frozenset[str]) -> str:
    """One operation of a startup warning, with the upstream scopes it still lacks."""
    if not missing:
        return f'{operation_id} needs a non-OAuth credential'
    return f'{operation_id} needs {" ".join(sorted(missing))}'
