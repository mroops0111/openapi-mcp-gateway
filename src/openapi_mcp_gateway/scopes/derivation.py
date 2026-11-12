import abc
import collections.abc
import logging
import re
import typing

from ..openapi import OperationInfo
from ..settings import SCOPE_TOKEN


logger = logging.getLogger(__name__)


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
    Nor does a tag that still makes no valid scope, such as one with non-ASCII letters,
    which is warned about once so it can be covered by a hand-written scope instead.
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

    def __init__(self) -> None:
        self._unusable_tags: set[str] = set()

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
            if self._is_usable(tag)
        )

    def _is_usable(self, tag: str) -> bool:
        """Whether ``tag`` makes a valid scope name, warning the first time one does not."""
        if SCOPE_TOKEN.fullmatch(_scope_token(tag)):
            return True
        if tag not in self._unusable_tags:
            self._unusable_tags.add(tag)
            logger.warning(
                'Tag %r makes no valid scope name, so auth.operation_scopes.auto tags derives no scope from it. '
                'Cover its operations with auth.operation_scopes.manual instead.',
                tag,
            )
        return False


# The extension point.
# Adding a derivation means adding a strategy above, one entry here, and its value in ``OperationScopesConfig.auto``.
SCOPE_DERIVATIONS: dict[str, type[ScopeDerivation]] = {
    'none': NoDerivation,
    'tags': TagMethodDerivation,
}


def _scope_token(tag: str) -> str:
    """Turn a tag into the part of a scope name it supplies."""
    return re.sub(r'\s+', '-', tag.strip()).lower()
