from mcp.server.auth.middleware.auth_context import get_access_token

from ..openapi import OperationInfo
from .policy import ScopePolicy
from .requirement import ScopeRequirement, Unrestricted


# What a name nobody registered requires.
_UNREGISTERED = Unrestricted()


class OperationAccess:
    """What each exposed name requires of a token's scopes, filled in as tools and operations are registered.

    A name it never registered, such as a dynamic meta-tool, requires nothing,
    since the operations behind it are what the scopes guard.
    """

    def __init__(self, policy: ScopePolicy) -> None:
        self.policy = policy
        self._requirements: dict[str, ScopeRequirement] = {}

    def register(self, name: str, operation: OperationInfo) -> ScopeRequirement:
        """Record what ``operation``, exposed as ``name``, requires, and return it."""
        requirement = self.policy.requirement_for(operation)
        self._requirements[name] = requirement
        return requirement

    def requirement(self, name: str) -> ScopeRequirement:
        """What a token needs to use ``name``."""
        return self._requirements.get(name, _UNREGISTERED)

    def permits(self, name: str, granted: frozenset[str]) -> bool:
        """Whether a token carrying ``granted`` may list and call ``name``."""
        return self.requirement(name).is_met(granted)


def granted_mcp_scopes() -> frozenset[str]:
    """The scopes of the token on the request being handled, or none outside an authenticated request."""
    access_token = get_access_token()
    return frozenset(access_token.scopes) if access_token else frozenset()
