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


class AnyScope(ScopeRequirement):
    """Met by a token carrying any one of ``scopes``.

    Several scopes covering one operation add up rather than narrow each other, so holding any of them is enough.
    """

    def __init__(self, scopes: collections.abc.Iterable[str]) -> None:
        self._scopes = tuple(sorted(set(scopes)))
        if not self._scopes:
            raise ValueError('AnyScope needs at least one scope')

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return not granted.isdisjoint(self._scopes)

    @property
    def challenge(self) -> tuple[str, ...]:
        return self._scopes

    @property
    def refusal(self) -> str:
        return f'This operation needs one of these scopes: {", ".join(self._scopes)}.'


class Unrestricted(ScopeRequirement):
    """Met by every token, for an operation no scope covers on a server that lets such operations through."""

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return True

    @property
    def challenge(self) -> tuple[str, ...]:
        return ()

    @property
    def refusal(self) -> str:
        return ''


class Unreachable(ScopeRequirement):
    """Met by no token, for an operation no scope covers on a server that refuses such operations."""

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return False

    @property
    def challenge(self) -> tuple[str, ...]:
        return ()

    @property
    def refusal(self) -> str:
        return 'No scope grants this operation.'
