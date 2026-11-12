import abc
import collections.abc
import logging
import typing

from ..openapi import OperationInfo
from ..policy import matches_pattern, unmatched_patterns
from ..settings import OperationScopesConfig
from .derivation import SCOPE_DERIVATIONS, ScopeDerivation
from .requirement import AnyScope, ScopeRequirement, Unreachable, Unrestricted


logger = logging.getLogger(__name__)


# How many operation names a startup log lists before it only counts the rest.
_LOGGED_OPERATION_LIMIT = 5

# What an operation no scope covers requires, keyed by ``auth.operation_scopes.unscoped``.
_UNSCOPED_REQUIREMENTS: dict[str, ScopeRequirement] = {
    'deny': Unreachable(),
    'allow': Unrestricted(),
}


class ScopePolicy(abc.ABC):
    """Which scopes a server defines, and what each operation requires of a token's scopes."""

    @property
    @abc.abstractmethod
    def defined_mcp_scopes(self) -> collections.abc.Mapping[str, str]:
        """Every scope the server defines, mapped to the description a person approving it is shown."""

    @property
    @abc.abstractmethod
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        """The cache scope of the tool and resource lists, which is private once they depend on who asks."""

    @abc.abstractmethod
    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        """What a token's scopes must contain for ``operation`` to be listed and called."""


class UnrestrictedPolicy(ScopePolicy):
    """No operation scopes, so every token may use every operation, as on a server without ``auth.operation_scopes``."""

    @property
    def defined_mcp_scopes(self) -> collections.abc.Mapping[str, str]:
        return {}

    @property
    def list_cache_scope(self) -> typing.Literal['public', 'private']:
        return 'public'

    def requirement_for(self, operation: OperationInfo) -> ScopeRequirement:
        return Unrestricted()


class ScopeCatalog(ScopePolicy):
    """The scopes ``auth.operation_scopes`` defines over a server's operations, derived and hand-written alike.

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
        return AnyScope(covering) if covering else self._unscoped

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
        shown = ', '.join(unscoped[:_LOGGED_OPERATION_LIMIT]) + (
            '...' if len(unscoped) > _LOGGED_OPERATION_LIMIT else ''
        )
        logger.log(
            logging.WARNING if self._config.unscoped == 'deny' else logging.INFO,
            'Server "%s": %d operation(s) no scope covers (%s) follow auth.operation_scopes.unscoped %s.',
            self._server_name,
            len(unscoped),
            shown,
            self._config.unscoped,
        )


def build_mcp_scope_policy(
    config: OperationScopesConfig | None,
    operations: collections.abc.Sequence[OperationInfo],
    server_name: str,
) -> ScopePolicy:
    """Return the scope policy for a server, which restricts nothing unless ``auth.operation_scopes`` is set."""
    if config is None:
        return UnrestrictedPolicy()
    return ScopeCatalog(config, operations, server_name)
