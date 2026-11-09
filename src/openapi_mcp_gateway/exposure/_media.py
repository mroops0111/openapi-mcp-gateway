import abc
import base64
import binascii
import copy
import typing

from mcp.types import ContentBlock

from ..field_path import FieldMatch, FieldPath
from ..openapi import MediaField, ResponseMedia
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
        """Decode ``value``, leaving anything that is not a valid base64 string alone.

        Whitespace is dropped first, since MIME base64 breaks its lines, as GitHub's contents API does.
        """
        if not isinstance(value, str):
            return None
        try:
            data = base64.b64decode(''.join(value.split()), validate=True)
        except binascii.Error:
            return None
        return binary_content(data, media_type, uri)


class MediaPromoter:
    """Move the media fields of a JSON payload into content blocks, leaving a placeholder in each field.

    The fields are those the response schema declares, found in each payload,
    and those a tool override names by field path, which win over the schema wherever both reach the same value.
    Built once per operation, so every path is parsed at startup rather than on each call.
    """

    # The encoding behind each ``MediaField.encoding`` value.
    # A new encoding is a new ``MediaEncoding`` here and a new value there, with nothing else to change.
    _ENCODINGS: typing.ClassVar[dict[str, MediaEncoding]] = {
        'base64': Base64Encoding(),
    }

    def __init__(self, response_media: ResponseMedia | None, overrides: typing.Mapping[str, MediaField]):
        self._response_media = response_media
        self._overrides = [(FieldPath.parse(path), field) for path, field in overrides.items()]

    def promote(self, payload: typing.Any, uri: str) -> tuple[typing.Any, list[ContentBlock]]:
        """Return ``payload`` with its media replaced by placeholders, and the content blocks, in order.

        ``payload`` itself is never changed, since it is copied first whenever it holds a value to promote.
        Blocks follow the payload order of the fields the schema declares, then the override fields the schema missed.
        ``uri`` names the upstream response, and each embedded resource is addressed by its field path within it.
        A path that matches nothing, or a value its encoding cannot read, is left as it is.
        """
        if self._response_media is None and not self._overrides:
            return payload, []
        found = self._find(payload)
        if not found:
            return payload, []
        # The copy records each container's copy by the original's id, so a match is replaced there without a new walk.
        copies: dict[int, typing.Any] = {}
        promoted = copy.deepcopy(payload, copies)
        contents: list[ContentBlock] = []
        for match, field in found.values():
            content = self._ENCODINGS[field.encoding].to_content(match.value, field.media_type, f'{uri}#{match.path}')
            if content is None:
                continue
            contents.append(content)
            placeholder = _PLACEHOLDER_TEMPLATE.format(index=len(contents), media_type=field.media_type)
            copies[id(match.container)][match.key] = placeholder
        return promoted, contents

    def _find(self, payload: typing.Any) -> dict[str, tuple[FieldMatch, MediaField]]:
        """Return every value to promote, keyed by its concrete path, so a value reached twice is promoted once."""
        found: dict[str, tuple[FieldMatch, MediaField]] = {}
        if self._response_media is not None:
            found.update((match.path, (match, field)) for match, field in self._response_media.find(payload))
        for path, field in self._overrides:
            for match in path.find(payload):
                found[match.path] = (match, field)
        return found
