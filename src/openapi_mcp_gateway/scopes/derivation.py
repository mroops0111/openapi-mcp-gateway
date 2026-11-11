import abc
import collections.abc
import re
import typing

from ..openapi import OperationInfo


class DerivedScope(typing.NamedTuple):
    """A scope derived from the spec, with the description a person approving it is shown."""

    name: str
    description: str


class ScopeDerivation(abc.ABC):
    """Strategy that derives the MCP scopes covering one operation from the spec, selected by ``auth.operation_scopes.auto``."""

    @abc.abstractmethod
    def derive(self, operation: OperationInfo) -> collections.abc.Iterable[DerivedScope]:
        """Return the scopes that cover ``operation``, or nothing when it derives none."""


class NoDerivation(ScopeDerivation):
    """Derive nothing, so only hand-written scopes cover operations."""

    def derive(self, operation: OperationInfo) -> collections.abc.Iterable[DerivedScope]:
        return ()


class TagMethodDerivation(ScopeDerivation):
    """Derive ``{tag}:read``, ``{tag}:write`` or ``{tag}:delete`` for each tag, by the operation's HTTP method.

    A tag is lowercased and its spaces become hyphens, since a scope cannot contain a space.
    A method outside the table, and an operation without tags, derive nothing.
    """

    _ACCESS_BY_METHOD: typing.ClassVar[dict[str, str]] = {
        'GET': 'read',
        'HEAD': 'read',
        'POST': 'write',
        'PUT': 'write',
        'PATCH': 'write',
        'DELETE': 'delete',
    }
    _DESCRIPTION_BY_ACCESS: typing.ClassVar[dict[str, str]] = {
        'read': 'Read {tag}',
        'write': 'Create and change {tag}',
        'delete': 'Delete {tag}',
    }

    def derive(self, operation: OperationInfo) -> collections.abc.Iterable[DerivedScope]:
        access = self._ACCESS_BY_METHOD.get(operation.method.upper())
        if access is None:
            return ()
        return tuple(
            DerivedScope(
                name=f'{_scope_token(tag)}:{access}',
                description=self._DESCRIPTION_BY_ACCESS[access].format(tag=tag),
            )
            for tag in operation.tags
        )


# The extension point.
# Adding a derivation means adding a strategy above, one entry here, and its value in ``OperationScopesConfig.auto``.
SCOPE_DERIVATIONS: dict[str, type[ScopeDerivation]] = {
    'none': NoDerivation,
    'tags': TagMethodDerivation,
}


def _scope_token(tag: str) -> str:
    """Turn a tag into the part of a scope name it supplies."""
    return re.sub(r'\s+', '-', tag.strip()).lower()
