import abc
import collections.abc

from ..openapi import OperationInfo
from .policy import McpScopePolicy, UpstreamSpecSecurityScopePolicy


class UpstreamScopeRequest(abc.ABC):
    """Which scopes a sign-in asks the upstream for, given the MCP scopes it will grant."""

    @abc.abstractmethod
    def scopes_for(self, mcp_scopes: collections.abc.Iterable[str]) -> list[str]:
        """The upstream scopes a sign-in granting ``mcp_scopes`` asks for."""


class UpstreamFixedScopeRequest(UpstreamScopeRequest):
    """The same scopes at every sign-in, whatever its MCP grant, as on a server without ``enforce_scopes``."""

    def __init__(self, scopes: collections.abc.Iterable[str]) -> None:
        self._scopes = list(scopes)

    def scopes_for(self, mcp_scopes: collections.abc.Iterable[str]) -> list[str]:
        return list(self._scopes)


class UpstreamDownscopedScopeRequest(UpstreamScopeRequest):
    """The upstream scopes the operations an MCP grant opens declare, asked for within ``ceiling``.

    ``ceiling`` is the most a sign-in may ask for, and the result keeps its order.
    A declared scope outside it is asked for through the ``ceiling`` scope implying it, or not at all.
    A ``ceiling`` scope that no operation declares, alone or through what it implies, is asked for at every sign-in,
    since it serves the sign-in itself, as ``offline_access`` and ``openid`` do.
    """

    def __init__(
        self,
        ceiling: collections.abc.Iterable[str],
        operations: collections.abc.Iterable[OperationInfo],
        mcp_policy: McpScopePolicy,
        spec_security: UpstreamSpecSecurityScopePolicy,
    ) -> None:
        self._ceiling = list(dict.fromkeys(ceiling))
        self._operations = tuple(operations)
        self._mcp_policy = mcp_policy
        self._spec_security = spec_security
        declared = set(spec_security.declared_scopes(self._operations))
        self._always_requested = frozenset(
            scope for scope in self._ceiling if declared.isdisjoint(spec_security.held_scopes([scope]))
        )

    def scopes_for(self, mcp_scopes: collections.abc.Iterable[str]) -> list[str]:
        granted = frozenset(mcp_scopes)
        opened = [
            operation for operation in self._operations if self._mcp_policy.requirement_for(operation).is_met(granted)
        ]
        needed = {self._covering_ceiling_scope(scope) for scope in self._spec_security.requested_scopes(opened)}
        return [scope for scope in self._ceiling if scope in needed or scope in self._always_requested]

    def _covering_ceiling_scope(self, scope: str) -> str | None:
        """The ``ceiling`` scope that asks for ``scope``, itself or one implying it, or ``None`` for neither."""
        if scope in self._ceiling:
            return scope
        return next((broader for broader in self._ceiling if scope in self._spec_security.held_scopes([broader])), None)


def build_upstream_scope_request(
    enforce_scopes: bool,
    ceiling: collections.abc.Sequence[str],
    operations: collections.abc.Sequence[OperationInfo],
    mcp_policy: McpScopePolicy,
    spec_security: UpstreamSpecSecurityScopePolicy,
) -> UpstreamScopeRequest:
    """Return how a server's sign-ins choose their upstream scopes, which follow the MCP grant under ``enforce_scopes``.

    Without it the spec's ``security`` is not trusted, so every sign-in asks for the whole ``ceiling``.
    """
    if not enforce_scopes:
        return UpstreamFixedScopeRequest(ceiling)
    return UpstreamDownscopedScopeRequest(ceiling, operations, mcp_policy, spec_security)
