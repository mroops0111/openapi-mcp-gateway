import abc
import collections.abc
import logging
import typing

from ..openapi import OperationInfo
from ..policy import matches_pattern, unmatched_patterns
from ..settings import OperationScopesConfig
from .derivation import SCOPE_DERIVATIONS, ScopeDerivation
from .requirement import (
    AnyScopeRequirement,
    AnyScopeSetRequirement,
    ScopeRequirement,
    UnreachableRequirement,
    UnrestrictedRequirement,
)


logger = logging.getLogger(__name__)


# How many operations a startup log lists before it only counts the rest.
_LOGGED_OPERATION_LIMIT = 5

# What an operation no MCP scope covers requires, keyed by ``auth.mcp.operation_scopes.unscoped``.
_UNSCOPED_REQUIREMENTS: dict[str, ScopeRequirement] = {
    'deny': UnreachableRequirement(),
    'allow': UnrestrictedRequirement(),
}

# The securitySchemes types whose requirements name OAuth scopes, the only credential the gateway holds upstream.
_OAUTH_SCHEME_TYPES = frozenset({'oauth2', 'openIdConnect'})


class ScopePolicy(abc.ABC):
    """What each operation requires of one kind of scope a token holds."""

    @property
    @abc.abstractmethod
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, which is private once they depend on who asks."""

    @abc.abstractmethod
    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        """What a token's scopes must contain for ``operation`` to be listed and called."""

    def held_scopes(self, granted: collections.abc.Iterable[str]) -> frozenset[str]:
        """The scopes a grant of ``granted`` holds, which are exactly those unless some imply others."""
        return frozenset(granted)


class McpScopePolicy(ScopePolicy):
    """What each operation requires of the MCP scopes the gateway grants a client, and which scopes it defines."""

    @property
    @abc.abstractmethod
    def mcp_defined_scopes(self) -> collections.abc.Mapping[str, str]:
        """Every scope the server defines, mapped to the description a person approving it is shown."""

    @property
    def opens_unscoped_operations(self) -> bool:
        """Whether an operation no scope covers is open to every token, as it is unless a policy closes it."""
        return True


class UpstreamScopePolicy(ScopePolicy):
    """What each operation requires of the scopes the upstream granted the user behind a token."""

    def sign_in_scopes(
        self,
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
        opened_operations: collections.abc.Iterable[OperationInfo],
    ) -> list[str]:
        """The upstream scopes a sign-in asks for to open ``opened_operations``.

        ``required_scopes`` are asked for at every sign-in, and the rest come from ``ceiling``.
        Every ``ceiling`` scope comes by default, for a policy that cannot tell which of them an operation needs.
        """
        return list(dict.fromkeys([*required_scopes, *ceiling]))


class UnrestrictedScopePolicy(ScopePolicy):
    """Every operation open whatever the token holds, so the lists are the same for every token."""

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'public'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return UnrestrictedRequirement()


class McpUnrestrictedScopePolicy(UnrestrictedScopePolicy, McpScopePolicy):
    """No MCP scopes, so every token may use every operation, as on a server without ``auth.mcp.operation_scopes``."""

    @property
    def mcp_defined_scopes(self) -> collections.abc.Mapping[str, str]:
        return {}


class McpConfiguredScopePolicy(McpScopePolicy):
    """The MCP scopes ``auth.mcp.operation_scopes`` defines over a server's operations, derived and hand-written alike.

    An operation opens to any scope that covers it, and falls to ``unscoped`` when none does.
    Construction refuses a hand-written scope that reuses a derived name,
    since the two definitions would silently merge into one.
    """

    def __init__(
        self,
        config: OperationScopesConfig,
        operations: collections.abc.Sequence[OperationInfo],
        server_name: str,
    ) -> None:
        self._config = config
        self._server_name = server_name
        self._derivation: ScopeDerivation = SCOPE_DERIVATIONS[config.auto]()
        self._unscoped: ScopeRequirement = _UNSCOPED_REQUIREMENTS[config.unscoped]
        derived = {
            scope.name: scope.description for operation in operations for scope in self._derivation.derive(operation)
        }
        self._refuse_reused_names(derived)
        self._mcp_defined_scopes = {**derived, **{name: scope.description for name, scope in config.manual.items()}}
        self._warn_on_unmatched_patterns(operations)
        self._log_unscoped(operations)

    @property
    def mcp_defined_scopes(self) -> collections.abc.Mapping[str, str]:
        return self._mcp_defined_scopes

    @property
    def opens_unscoped_operations(self) -> bool:
        return self._config.unscoped == 'allow'

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'private'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        covering = self._covering_scopes(operation)
        return AnyScopeRequirement(covering) if covering else self._unscoped

    def _covering_scopes(self, operation: OperationInfo) -> set[str]:
        derived = {scope.name for scope in self._derivation.derive(operation)}
        manual = {
            name
            for name, scope in self._config.manual.items()
            if any(matches_pattern(operation, pattern) for pattern in scope.operations)
        }
        return derived | manual

    def _refuse_reused_names(self, derived: collections.abc.Mapping[str, str]) -> None:
        """Refuse a hand-written scope named like a derived one, since the two would merge into one silently."""
        reused = sorted(set(derived) & set(self._config.manual))
        if reused:
            raise ValueError(
                f'Server "{self._server_name}": auth.mcp.operation_scopes.manual reuses names auth.mcp.operation_scopes.auto {self._config.auto} '
                f'derives ({", ".join(reused)}). Rename the hand-written scopes.'
            )

    def _warn_on_unmatched_patterns(self, operations: collections.abc.Sequence[OperationInfo]) -> None:
        """Warn about a hand-written pattern that matches no operation, which is almost always a typo."""
        patterns = [pattern for scope in self._config.manual.values() for pattern in scope.operations]
        stray = unmatched_patterns(list(operations), patterns)
        if stray:
            logger.warning(
                'Server "%s": %d auth.mcp.operation_scopes.manual pattern(s) matched no operation (%s). '
                'Check them against the operationIds in the spec.',
                self._server_name,
                len(stray),
                ', '.join(stray),
            )

    def _log_unscoped(self, operations: collections.abc.Sequence[OperationInfo]) -> None:
        """Report the operations no scope covers, since under ``deny`` they vanish from every client."""
        unscoped = [operation.operation_id for operation in operations if not self._covering_scopes(operation)]
        if not unscoped:
            return
        logger.log(
            logging.WARNING if self._config.unscoped == 'deny' else logging.INFO,
            'Server "%s": %d operation(s) no scope covers (%s) follow auth.mcp.operation_scopes.unscoped %s.',
            self._server_name,
            len(unscoped),
            _shorten(unscoped),
            self._config.unscoped,
        )


class UpstreamUnrestrictedScopePolicy(UnrestrictedScopePolicy, UpstreamScopePolicy):
    """Every operation open whatever the upstream granted, as on a server without ``auth.upstream.enforce_scopes``."""


class UpstreamSpecSecurityScopePolicy(UpstreamScopePolicy):
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
        return self._scope_sets_of(operation) or UnrestrictedRequirement()

    def held_scopes(self, granted: collections.abc.Iterable[str]) -> frozenset[str]:
        held = set(granted)
        pending = list(held)
        while pending:
            for implied in self._scope_implies.get(pending.pop(), ()):
                if implied not in held:
                    held.add(implied)
                    pending.append(implied)
        return frozenset(held)

    def declared_scopes(self, operations: collections.abc.Iterable[OperationInfo]) -> list[str]:
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

    def sign_in_scopes(
        self,
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
        opened_operations: collections.abc.Iterable[OperationInfo],
    ) -> list[str]:
        """``required_scopes``, then the ``ceiling`` scopes that ask for what ``opened_operations`` declare.

        OpenAPI lets any one security requirement of an operation authorize it, so each operation asks for one.
        It is the requirement the fewest scopes beyond ``required_scopes`` meet, counting what each scope implies,
        the first the spec lists among equals.
        A requirement no ``ceiling`` scope can meet is passed over, and an operation with none asks for nothing.
        Each scope of the chosen requirement is asked for itself when ``ceiling`` lists it,
        or else through the narrowest ``ceiling`` scope implying it.
        A chosen scope another chosen one implies is left out, since the broader one already covers it,
        and so is one a required scope implies.
        The chosen scopes keep the order of ``ceiling``.
        """
        ceiling_scopes = list(dict.fromkeys(ceiling))
        required_held_scopes = self.held_scopes(required_scopes)
        chosen_scopes = [
            scope
            for operation in opened_operations
            for scope in self._cheapest_covering(operation, ceiling_scopes, required_held_scopes)
        ]
        needed_scopes = {
            scope for scope in self._without_implied_scopes(chosen_scopes) if scope not in required_held_scopes
        }
        return list(dict.fromkeys([*required_scopes, *(scope for scope in ceiling_scopes if scope in needed_scopes)]))

    def warn_on_missing_oauth_scheme(self, server_name: str) -> None:
        """Warn that enforcing the upstream grant restricts nothing, when the spec declares no OAuth scheme."""
        if not self.oauth_schemes:
            logger.warning(
                'Server "%s": auth.upstream.enforce_scopes is on, but the spec declares no OAuth security scheme, '
                'so no operation is restricted by the upstream grant.',
                server_name,
            )

    def drop_unreachable_operations(
        self,
        operations: collections.abc.Sequence[OperationInfo],
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
        server_name: str,
    ) -> list[OperationInfo]:
        """Return ``operations`` less those no sign-in can open, warning about the ones left out.

        A sign-in asks for at most ``required_scopes`` and ``ceiling``,
        so an operation their scopes cannot meet is refused at every call.
        It is left out with a warning rather than refused at startup, since the spec may declare it wrongly.
        """
        held = self.held_scopes([*required_scopes, *ceiling])
        unmet = {
            operation.operation_id: requirement.missing(held)
            for operation in operations
            if (requirement := self._scope_sets_of(operation)) and not requirement.is_met(held)
        }
        if unmet:
            described = [_describe_unmet(operation_id, missing) for operation_id, missing in unmet.items()]
            logger.warning(
                'Server "%s": no sign-in can open %d operation(s) within auth.upstream.scopes '
                'and auth.upstream.required_scopes, so they are not exposed (%s). '
                'Add the scopes to auth.upstream.scopes, or correct an operation under operations.<id>.security.',
                server_name,
                len(described),
                _shorten(described),
            )
        return [operation for operation in operations if operation.operation_id not in unmet]

    def warn_on_unasked_ceiling_scopes(
        self,
        operations: collections.abc.Iterable[OperationInfo],
        ceiling: collections.abc.Sequence[str],
        server_name: str,
    ) -> None:
        """Warn about the ``ceiling`` scopes no sign-in asks for, since no operation declares what they cover.

        A scope a sign-in needs whatever it opens, such as one for a refresh token, belongs in ``required_scopes``.
        """
        declared_scopes = set(self.declared_scopes(operations))
        unasked_scopes = [scope for scope in ceiling if declared_scopes.isdisjoint(self.held_scopes([scope]))]
        if unasked_scopes:
            logger.warning(
                'Server "%s": no operation declares what auth.upstream.scopes %s cover, so no sign-in asks for them. '
                'Move a scope every sign-in needs to auth.upstream.required_scopes, '
                'or declare it under operations.<id>.security.',
                server_name,
                ' '.join(unasked_scopes),
            )

    def warn_on_empty_sign_in(
        self,
        operations: collections.abc.Iterable[OperationInfo],
        ceiling: collections.abc.Sequence[str],
        required_scopes: collections.abc.Sequence[str],
        mcp_policy: McpScopePolicy,
        server_name: str,
    ) -> None:
        """Warn when a sign-in granted no MCP scope asks the upstream for no scope at all.

        A wider MCP grant opens more operations, so it asks for at least as much,
        and the sign-in granted none is the only one to check.
        Its authorize request then carries no ``scope``, which some upstreams accept and others refuse.
        """
        opened_operations = [
            operation for operation in operations if mcp_policy.requirement_for(operation).is_met(frozenset())
        ]
        if not self.sign_in_scopes(ceiling, required_scopes, opened_operations):
            logger.warning(
                'Server "%s": a sign-in whose MCP scopes open only operations declaring no upstream scope '
                'asks the upstream for none, so its authorize request carries no scope parameter. '
                'An upstream that requires one refuses that sign-in. '
                'List the scopes every sign-in needs in auth.upstream.required_scopes, '
                'or declare what the operations need under operations.<id>.security.',
                server_name,
            )

    def warn_on_unrequested_scopes(
        self,
        operations: collections.abc.Sequence[OperationInfo],
        requested: collections.abc.Iterable[str],
        mcp_policy: McpScopePolicy,
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
                _shorten(described),
            )

    def _cheapest_covering(
        self,
        operation: OperationInfo,
        ceiling_scopes: collections.abc.Sequence[str],
        required_held_scopes: collections.abc.Set[str],
    ) -> list[str]:
        """The ``ceiling_scopes`` that meet the security requirement of ``operation`` asking least of a sign-in.

        Empty when ``operation`` needs no scope, when ``required_held_scopes`` already meet a requirement,
        or when no requirement can be met within ``ceiling_scopes``.
        """
        requirement = self._scope_sets_of(operation)
        if requirement is None:
            return []
        coverings: list[list[str]] = []
        for scope_set in requirement.scope_sets:
            covering = [
                self._covering_ceiling_scope(scope, ceiling_scopes)
                for scope in sorted(scope_set - required_held_scopes)
            ]
            if None not in covering:
                coverings.append([scope for scope in covering if scope is not None])
        return min(coverings, key=lambda covering: len(self.held_scopes(covering) - required_held_scopes), default=[])

    def _covering_ceiling_scope(self, scope: str, ceiling_scopes: collections.abc.Sequence[str]) -> str | None:
        """The ``ceiling_scopes`` scope that asks for ``scope``, or ``None`` when none does.

        That is ``scope`` itself when listed, or else the listed scope implying it that implies the fewest scopes,
        the first listed among equals.
        """
        if scope in ceiling_scopes:
            return scope
        broader_scopes = [broader for broader in ceiling_scopes if scope in self.held_scopes([broader])]
        return min(broader_scopes, key=lambda broader: len(self.held_scopes([broader])), default=None)

    def _without_implied_scopes(self, scopes: collections.abc.Iterable[str]) -> list[str]:
        """``scopes`` in order without repeats, less each one the others imply, since a broader one covers it.

        Scopes that imply each other are left out one at a time, so one of them always stays.
        """
        kept_scopes = list(dict.fromkeys(scopes))
        for scope in list(kept_scopes):
            if scope in self.held_scopes(other for other in kept_scopes if other != scope):
                kept_scopes.remove(scope)
        return kept_scopes

    def _scope_sets_of(self, operation: OperationInfo) -> AnyScopeSetRequirement | None:
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
        return AnyScopeSetRequirement(scope_sets, operation.operation_id)


def build_mcp_scope_policy(
    config: OperationScopesConfig | None,
    operations: collections.abc.Sequence[OperationInfo],
    server_name: str,
) -> McpScopePolicy:
    """Return the MCP scope policy for a server, which restricts nothing unless ``auth.mcp.operation_scopes`` is set."""
    if config is None:
        return McpUnrestrictedScopePolicy()
    return McpConfiguredScopePolicy(config, operations, server_name)


def build_upstream_scope_policy(
    enforce_scopes: bool, spec_security: UpstreamSpecSecurityScopePolicy
) -> UpstreamScopePolicy:
    """Return the upstream scope policy for a server, which restricts nothing unless ``enforce_scopes`` is on.

    A spec declaring no OAuth scheme restricts nothing either, as ``warn_on_missing_oauth_scheme`` tells the operator.
    """
    if not enforce_scopes or not spec_security.oauth_schemes:
        return UpstreamUnrestrictedScopePolicy()
    return spec_security


def _describe_unmet(operation_id: str, missing: frozenset[str]) -> str:
    """One operation of a startup warning, with the upstream scopes it still lacks."""
    if not missing:
        return f'{operation_id} needs a non-OAuth credential'
    return f'{operation_id} needs {" ".join(sorted(missing))}'


def _shorten(items: collections.abc.Sequence[str]) -> str:
    """The first items of a startup log list, with an ellipsis when more were left out."""
    return ', '.join(items[:_LOGGED_OPERATION_LIMIT]) + ('...' if len(items) > _LOGGED_OPERATION_LIMIT else '')
