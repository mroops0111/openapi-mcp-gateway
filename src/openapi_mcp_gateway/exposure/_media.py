import abc
import base64
import binascii
import copy
import dataclasses
import typing

from mcp.types import ContentBlock

from ..field_path import FieldPath
from ..openapi import MediaField
from ._content import binary_content


# What a promoted field holds once its media moves to a content block, so the model can tell which block it was.
# The JSON text is ``content[0]``, so the first promoted block is ``content[1]``.
_PLACEHOLDER_TEMPLATE = '<content[{index}]: {media_type}>'


class MediaEncoding(abc.ABC):
    """Strategy for how a field carries media, which decides how its value becomes a content block."""

    @abc.abstractmethod
    def to_content(self, value: typing.Any, media_type: str, uri: str) -> ContentBlock | None:
        """Return the content block ``value`` carries, or ``None`` when it carries none in this encoding."""


class Base64Encoding(MediaEncoding):
    """A field holding the bytes as a base64 string."""

    def to_content(self, value: typing.Any, media_type: str, uri: str) -> ContentBlock | None:
        """Decode ``value``, leaving anything that is not a valid base64 string alone."""
        if not isinstance(value, str):
            return None
        try:
            data = base64.b64decode(value, validate=True)
        except binascii.Error:
            return None
        return binary_content(data, media_type, uri)


@dataclasses.dataclass(frozen=True)
class _MediaRule:
    """One declared media field, ready to apply."""

    path: FieldPath
    media_type: str
    encoding: MediaEncoding


class MediaPromoter:
    """Move the declared media fields of a JSON payload into content blocks, leaving a placeholder in each field.

    Built once per operation, so every path is parsed at startup rather than on each call.
    """

    # The encoding behind each ``MediaField.encoding`` value.
    # A new encoding is a new ``MediaEncoding`` here and a new value there, with nothing else to change.
    _ENCODINGS: typing.ClassVar[dict[str, MediaEncoding]] = {
        'base64': Base64Encoding(),
    }

    def __init__(self, fields: typing.Mapping[str, MediaField]):
        self._rules = [
            _MediaRule(FieldPath.parse(path), field.media_type, self._ENCODINGS[field.encoding])
            for path, field in fields.items()
        ]

    def promote(self, payload: typing.Any, uri: str) -> tuple[typing.Any, list[ContentBlock]]:
        """Return a copy of ``payload`` with its media replaced by placeholders, and the content blocks, in order.

        ``uri`` names the upstream response, and each embedded resource is addressed by its field path within it.
        A path that matches nothing, or a value its encoding cannot read, is left as it is.
        """
        if not self._rules:
            return payload, []
        payload = copy.deepcopy(payload)
        contents: list[ContentBlock] = []
        for rule in self._rules:
            for match in rule.path.find(payload):
                content = rule.encoding.to_content(match.value, rule.media_type, f'{uri}#{match.path}')
                if content is None:
                    continue
                contents.append(content)
                match.replace(_PLACEHOLDER_TEMPLATE.format(index=len(contents), media_type=rule.media_type))
        return payload, contents
