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

# What an operation no MCP scope covers requires, keyed by ``auth.operation_scopes.unscoped``.
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
    def defined_mcp_scopes(self) -> collections.abc.Mapping[str, str]:
        """Every scope the server defines, mapped to the description a person approving it is shown."""


class UpstreamScopePolicy(ScopePolicy):
    """What each operation requires of the scopes the upstream granted the user behind a token."""


class UnrestrictedMcpScopePolicy(McpScopePolicy):
    """No MCP scopes, so every token may use every operation, as on a server without ``auth.operation_scopes``."""

    @property
    def defined_mcp_scopes(self) -> collections.abc.Mapping[str, str]:
        return {}

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'public'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return UnrestrictedRequirement()


class ConfiguredMcpScopePolicy(McpScopePolicy):
    """The MCP scopes ``auth.operation_scopes`` defines over a server's operations, derived and hand-written alike.

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
        self._defined_mcp_scopes = {**derived, **{name: scope.description for name, scope in config.manual.items()}}
        self._warn_on_unmatched_patterns(operations)
        self._log_unscoped(operations)

    @property
    def defined_mcp_scopes(self) -> collections.abc.Mapping[str, str]:
        return self._defined_mcp_scopes

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
                f'Server "{self._server_name}": auth.operation_scopes.manual reuses names auth.operation_scopes.auto {self._config.auto} '
                f'derives ({", ".join(reused)}). Rename the hand-written scopes.'
            )

    def _warn_on_unmatched_patterns(self, operations: collections.abc.Sequence[OperationInfo]) -> None:
        """Warn about a hand-written pattern that matches no operation, which is almost always a typo."""
        patterns = [pattern for scope in self._config.manual.values() for pattern in scope.operations]
        stray = unmatched_patterns(list(operations), patterns)
        if stray:
            logger.warning(
                'Server "%s": %d auth.operation_scopes.manual pattern(s) matched no operation (%s). '
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
            'Server "%s": %d operation(s) no scope covers (%s) follow auth.operation_scopes.unscoped %s.',
            self._server_name,
            len(unscoped),
            _shorten(unscoped),
            self._config.unscoped,
        )


class UnrestrictedUpstreamScopePolicy(UpstreamScopePolicy):
    """Every operation open whatever the upstream granted, as on a server without ``auth.upstream.enforce_scopes``."""

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'public'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return UnrestrictedRequirement()


class SpecSecurityUpstreamScopePolicy(UpstreamScopePolicy):
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
    """Return the MCP scope policy for a server, which restricts nothing unless ``auth.operation_scopes`` is set."""
    if config is None:
        return UnrestrictedMcpScopePolicy()
    return ConfiguredMcpScopePolicy(config, operations, server_name)


def build_upstream_scope_policy(
    enforce_scopes: bool, spec_security: SpecSecurityUpstreamScopePolicy, server_name: str
) -> UpstreamScopePolicy:
    """Return the upstream scope policy for a server, which restricts nothing unless ``enforce_scopes`` is on."""
    if not enforce_scopes:
        return UnrestrictedUpstreamScopePolicy()
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


def _shorten(items: collections.abc.Sequence[str]) -> str:
    """The first items of a startup log list, with an ellipsis when more were left out."""
    return ', '.join(items[:_LOGGED_OPERATION_LIMIT]) + ('...' if len(items) > _LOGGED_OPERATION_LIMIT else '')
