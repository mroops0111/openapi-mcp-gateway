import base64

from mcp.types import AudioContent, BlobResourceContents, ContentBlock, EmbeddedResource, ImageContent


# The content blocks that carry binary data.
BinaryContent = ImageContent | AudioContent | EmbeddedResource


def binary_content(data: bytes, media_type: str, uri: str) -> BinaryContent:
    """Wrap ``data`` as the content block ``media_type`` calls for.

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


def binary_content_data(content: ContentBlock) -> bytes | None:
    """Return the bytes ``content`` carries, or ``None`` when it carries none, the inverse of ``binary_content``."""
    if isinstance(content, ImageContent | AudioContent):
        return base64.b64decode(content.data)
    if isinstance(content, EmbeddedResource) and isinstance(content.resource, BlobResourceContents):
        return base64.b64decode(content.resource.blob)
    return None
