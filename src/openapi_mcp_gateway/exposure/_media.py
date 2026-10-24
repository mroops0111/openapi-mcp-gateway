import base64
import binascii
import copy
import re
import typing

from mcp.types import AudioContent, BlobResourceContents, EmbeddedResource, ImageContent

from ..openapi import MediaField


# One step of a media field path, which is a property name or ``[*]`` for every item of an array.
_PATH_STEP = re.compile(r'\[\*\]|[^.\[\]]+')
_EVERY_ITEM = '[*]'

# What a promoted field holds in the JSON once its bytes move to a content block,
# so the model can tell which block belongs to which field.
_PLACEHOLDER = '<content[{index}]: {media_type}, {size} bytes>'

# The content blocks a piece of media can become.
MediaBlock = ImageContent | AudioContent | EmbeddedResource


def media_block(data: bytes, media_type: str, uri: str) -> MediaBlock:
    """Wrap ``data`` as the MCP content block ``media_type`` calls for.

    Images and audio get their own block types, so a client can render or play them.
    Anything else, such as a PDF or an archive, is embedded as a blob resource under ``uri``.
    """
    encoded = base64.b64encode(data).decode('ascii')
    if media_type.startswith('image/'):
        return ImageContent(type='image', data=encoded, mime_type=media_type)
    if media_type.startswith('audio/'):
        return AudioContent(type='audio', data=encoded, mime_type=media_type)
    return EmbeddedResource(
        type='resource',
        resource=BlobResourceContents(uri=uri, blob=encoded, mime_type=media_type),
    )


def promote_media(payload: typing.Any, fields: dict[str, MediaField], url: str) -> tuple[typing.Any, list[MediaBlock]]:
    """Move the media in ``fields`` out of ``payload`` into content blocks, leaving a placeholder in each field.

    Returns a copy of ``payload`` and the blocks, numbered from ``content[1]`` since the JSON text is ``content[0]``.
    A path that matches nothing, or a value that is not valid base64, is left as it is.
    """
    if not fields:
        return payload, []
    payload = copy.deepcopy(payload)
    blocks: list[MediaBlock] = []
    for path, field in fields.items():
        steps = _PATH_STEP.findall(path)
        if not steps:
            continue
        for container, key, location in _locate(payload, steps, ''):
            value = container[key]
            if not isinstance(value, str):
                continue
            try:
                data = base64.b64decode(value, validate=True)
            except binascii.Error:
                continue
            blocks.append(media_block(data, field.media_type, f'{url}#{location}'))
            container[key] = _PLACEHOLDER.format(index=len(blocks), media_type=field.media_type, size=len(data))
    return payload, blocks


def _locate(node: typing.Any, steps: list[str], location: str) -> typing.Iterator[tuple[typing.Any, str | int, str]]:
    """Yield the container, key and concrete location of every value ``steps`` reaches from ``node``.

    The location spells out each array index, so two attachments under one ``[*]`` path stay apart.
    """
    step, rest = steps[0], steps[1:]
    if step == _EVERY_ITEM:
        if not isinstance(node, list):
            return
        keys: list[str | int] = list(range(len(node)))
    elif isinstance(node, dict) and step in node:
        keys = [step]
    else:
        return
    for key in keys:
        child_location = f'{location}[{key}]' if isinstance(key, int) else f'{location}.{key}' if location else key
        if rest:
            yield from _locate(node[key], rest, child_location)
        else:
            yield node, key, child_location
