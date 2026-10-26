import dataclasses
import re
import typing


# One segment of a field path, which is a property name or the wildcard for every item of an array.
_SEGMENT_PATTERN = re.compile(r'\[\*\]|[^.\[\]]+')
ARRAY_WILDCARD = '[*]'


@dataclasses.dataclass(frozen=True)
class FieldMatch:
    """One value a ``FieldPath`` reaches in a payload, held by its container so it can be replaced in place."""

    container: dict[str, typing.Any] | list[typing.Any]
    key: str | int
    # The concrete path to the value, with each array index spelled out, such as ``attachments[0].content``.
    path: str

    @property
    def value(self) -> typing.Any:
        """The value at this match."""
        return typing.cast(typing.Any, self.container)[self.key]

    def replace(self, value: typing.Any) -> None:
        """Put ``value`` where the matched value was."""
        typing.cast(typing.Any, self.container)[self.key] = value


@dataclasses.dataclass(frozen=True)
class FieldPath:
    """A path to fields in a JSON payload, written as dot-separated property names with ``[*]`` for every array item.

    ``attachments[*].content`` reaches the ``content`` of each item of the ``attachments`` array.
    """

    segments: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> typing.Self:
        """Parse ``text``, raising ``ValueError`` unless it is a non-empty path in exactly this syntax."""
        path = cls(tuple(_SEGMENT_PATTERN.findall(text)))
        if not path.segments or str(path) != text:
            raise ValueError(
                f'Invalid field path {text!r}: use property names joined by dots, with [*] for array items'
            )
        return path

    def child(self, name: str) -> typing.Self:
        """Return the path to property ``name`` of the value this path reaches."""
        return dataclasses.replace(self, segments=(*self.segments, name))

    def each_item(self) -> typing.Self:
        """Return the path to every item of the array this path reaches."""
        return dataclasses.replace(self, segments=(*self.segments, ARRAY_WILDCARD))

    def find(self, payload: typing.Any) -> typing.Iterator[FieldMatch]:
        """Yield every value this path reaches in ``payload``, skipping any branch whose shape does not match."""
        if self.segments:
            yield from self._find(payload, self.segments, '')

    def __str__(self) -> str:
        """Render the path in the syntax ``parse`` reads."""
        return ''.join(
            segment if segment == ARRAY_WILDCARD or index == 0 else f'.{segment}'
            for index, segment in enumerate(self.segments)
        )

    @classmethod
    def _find(cls, node: typing.Any, segments: tuple[str, ...], path: str) -> typing.Iterator[FieldMatch]:
        """Yield the matches for ``segments`` under ``node``, whose own concrete path is ``path``."""
        segment, rest = segments[0], segments[1:]
        if segment == ARRAY_WILDCARD:
            if not isinstance(node, list):
                return
            keys: list[str | int] = list(range(len(node)))
        elif isinstance(node, dict) and segment in node:
            keys = [segment]
        else:
            return
        for key in keys:
            child_path = f'{path}[{key}]' if isinstance(key, int) else f'{path}.{key}' if path else key
            if rest:
                yield from cls._find(typing.cast(typing.Any, node)[key], rest, child_path)
            else:
                yield FieldMatch(node, key, child_path)
