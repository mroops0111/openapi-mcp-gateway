import base64

import httpx
import pytest
from mcp.server import MCPServer
from mcp.types import AudioContent, BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent

from openapi_mcp_gateway.exposure import (
    ToolGenerator,
    UpstreamConnection,
)
from openapi_mcp_gateway.openapi import (
    OperationInfo,
)
from tests.constants import API_URL
from tests.served_tool import ServedTool


class TestBinaryResponses:
    """A binary upstream body reaches the client as the MCP content block its media type calls for."""

    _BODY = b'\x89PNG\r\n\x1a\n\xff\xfe'

    async def _call(self, mock_upstream, content_type: str) -> CallToolResult:
        """Call a one-operation tool whose upstream answers with ``_BODY`` under ``content_type``."""
        mock_upstream(lambda _request: httpx.Response(200, content=self._BODY, headers={'content-type': content_type}))
        mcp = MCPServer('test')
        ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register(
            [
                OperationInfo(operation_id='download', method='get', path='/files/report'),
            ]
        )
        tool = ServedTool(mcp, 'download')
        return await tool.call({})

    @pytest.mark.parametrize(
        ('content_type', 'block_type'), [('image/png', ImageContent), ('audio/mpeg', AudioContent)]
    )
    async def test_image_and_audio_get_their_own_blocks(self, mock_upstream, content_type, block_type):
        """Images and audio arrive as base64 in an image or audio block, with the upstream media type."""
        result = await self._call(mock_upstream, content_type)

        (block,) = result.content
        assert isinstance(block, ImageContent | AudioContent)
        assert isinstance(block, block_type)
        assert block.mime_type == content_type
        assert base64.b64decode(block.data) == self._BODY
        assert result.structured_content is None

    async def test_other_binary_is_an_embedded_blob(self, mock_upstream):
        """Anything else, such as a PDF, arrives as a blob resource under the URL it was fetched from."""
        result = await self._call(mock_upstream, 'application/pdf')

        (block,) = result.content
        assert isinstance(block, EmbeddedResource)
        assert isinstance(block.resource, BlobResourceContents)
        assert block.resource.uri == f'{API_URL}/files/report'
        assert block.resource.mime_type == 'application/pdf'
        assert base64.b64decode(block.resource.blob) == self._BODY
