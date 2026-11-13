import abc
import collections.abc


class ScopeRequirement(abc.ABC):
    """What a token's scopes must contain for one operation to be listed and called."""

    @abc.abstractmethod
    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        """Whether a token carrying ``granted`` may use the operation."""

    @property
    @abc.abstractmethod
    def challenge(self) -> tuple[str, ...]:
        """The scopes a refusal names, so a client knows what to ask for. Empty when no scope would help."""

    @property
    @abc.abstractmethod
    def refusal(self) -> str:
        """Why a token that does not meet this requirement is refused, as shown to the caller."""


class AnyScopeRequirement(ScopeRequirement):
    """Met by a token carrying any one of ``scopes``.

    Several scopes covering one operation add up rather than narrow each other, so holding any of them is enough.
    """

    def __init__(self, scopes: collections.abc.Iterable[str]) -> None:
        self._scopes = tuple(sorted(set(scopes)))
        if not self._scopes:
            raise ValueError('AnyScopeRequirement needs at least one scope')

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return not granted.isdisjoint(self._scopes)

    @property
    def challenge(self) -> tuple[str, ...]:
        return self._scopes

    @property
    def refusal(self) -> str:
        return f'This operation needs one of these scopes: {", ".join(self._scopes)}.'


class AnyScopeSetRequirement(ScopeRequirement):
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


class UnrestrictedRequirement(ScopeRequirement):
    """Met by every token, for an operation no scope covers on a server that lets such operations through."""

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return True

    @property
    def challenge(self) -> tuple[str, ...]:
        return ()

    @property
    def refusal(self) -> str:
        return ''


class UnreachableRequirement(ScopeRequirement):
    """Met by no token, for an operation no scope covers on a server that refuses such operations."""

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return False

    @property
    def challenge(self) -> tuple[str, ...]:
        return ()

    @property
    def refusal(self) -> str:
        return 'No scope grants this operation.'
