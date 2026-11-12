from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.uri_template import UriTemplate

from ..openapi import OperationInfo
from .policy import ScopePolicy
from .requirement import ScopeRequirement, Unrestricted


# What a name nobody registered requires.
_UNREGISTERED = Unrestricted()


class OperationAccess:
    """What each exposed name requires of a token's scopes, filled in as tools, operations and resources are registered.

    A tool or operation is known by its name, and a resource by its URI or URI template.
    A concrete URI a template expands to requires what the template does.
    A name it never registered, such as a dynamic meta-tool, requires nothing,
    since the operations behind it are what the scopes guard.
    """

    def __init__(self, policy: ScopePolicy) -> None:
        self.policy = policy
        self._requirements: dict[str, ScopeRequirement] = {}
        self._resource_templates: list[tuple[UriTemplate, ScopeRequirement]] = []

    def register(self, name: str, operation: OperationInfo) -> ScopeRequirement:
        """Record what ``operation``, exposed as the tool or operation ``name``, requires, and return it."""
        requirement = self.policy.requirement_for(operation)
        self._requirements[name] = requirement
        return requirement

    def register_resource(self, uri_template: str, operation: OperationInfo) -> ScopeRequirement:
        """Record what ``operation``, exposed as a resource at ``uri_template``, requires, and return it."""
        requirement = self.register(uri_template, operation)
        if UriTemplate.is_template(uri_template):
            self._resource_templates.append((UriTemplate.parse(uri_template), requirement))
        return requirement

    def requirement(self, name: str) -> ScopeRequirement:
        """What a token needs to use ``name``, a tool, an operation, a resource URI or a resource template."""
        if name in self._requirements:
            return self._requirements[name]
        return next(
            (requirement for template, requirement in self._resource_templates if template.match(name) is not None),
            _UNREGISTERED,
        )

    def permits(self, name: str, granted: frozenset[str]) -> bool:
        """Whether a token carrying ``granted`` may list and use ``name``."""
        return self.requirement(name).is_met(granted)


def granted_mcp_scopes() -> frozenset[str]:
    """The scopes of the token on the request being handled, or none outside an authenticated request."""
    access_token = get_access_token()
    return frozenset(access_token.scopes) if access_token else frozenset()
