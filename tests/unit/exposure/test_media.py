import base64
import typing

import httpx
from mcp.server import MCPServer
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent

from openapi_mcp_gateway.exposure import (
    ToolGenerator,
    UpstreamConnection,
)
from openapi_mcp_gateway.openapi import (
    McpIntegration,
    MediaField,
    OperationInfo,
    ToolOverride,
    parse_spec,
)
from tests.constants import API_URL
from tests.context import stub_context
from tests.specs import operation_spec


class TestMediaFields:
    """Media fields of a JSON response move into content blocks, leaving a placeholder that points at each one."""

    _PNG = b'\x89PNG\r\n\x1a\n'
    _PDF = b'%PDF-1.7'

    async def _call(
        self,
        mock_upstream,
        body: typing.Any,
        response: dict[str, typing.Any] | None = None,
        tool: ToolOverride | None = None,
    ) -> CallToolResult:
        """Call a one-operation tool whose upstream answers with the JSON ``body``, as the schema ``response`` says."""
        mock_upstream(lambda _request: httpx.Response(200, json=body))
        mcp = MCPServer('test')
        spec_operation = parse_spec(operation_spec(response=response)).operations[0]
        operation = OperationInfo(
            operation_id='get_contract',
            method='get',
            path='/contracts/7',
            response_media=spec_operation.response_media,
            x_mcp_integration=McpIntegration(tool=tool),
        )
        ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation])
        registered = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'get_contract')
        return await registered.run({}, context=stub_context())

    @staticmethod
    def _encode(data: bytes) -> str:
        return base64.b64encode(data).decode('ascii')

    @staticmethod
    def _object(**properties: dict[str, typing.Any]) -> dict[str, typing.Any]:
        return {'type': 'object', 'properties': properties}

    @staticmethod
    def _base64(media_type: str | None = None) -> dict[str, typing.Any]:
        schema = {'type': 'string', 'contentEncoding': 'base64'}
        return {**schema, 'contentMediaType': media_type} if media_type else schema

    async def test_declared_field_follows_the_json_as_a_block(self, mock_upstream):
        """The JSON keeps its shape with a placeholder, and the bytes follow it as an image block."""
        body = {'id': 7, 'signature': {'image': self._encode(self._PNG)}}
        response = self._object(signature=self._object(image=self._base64('image/png')))

        result = await self._call(mock_upstream, body, response)

        text, image = result.content
        assert isinstance(text, TextContent)
        assert isinstance(image, ImageContent)
        assert base64.b64decode(image.data) == self._PNG
        placeholder = '<content[1]: image/png>'
        assert result.structured_content == {'id': 7, 'signature': {'image': placeholder}}
        assert placeholder in text.text

    async def test_every_array_item_gets_its_own_block(self, mock_upstream):
        """Each item under ``[*]`` becomes a separate block, numbered and addressed by its index."""
        body = {'attachments': [{'content': self._encode(self._PDF)}, {'content': self._encode(self._PNG)}]}
        attachment = self._object(content=self._base64('application/pdf'))
        response = self._object(attachments={'type': 'array', 'items': attachment})

        result = await self._call(mock_upstream, body, response)

        _text, *blocks = result.content
        assert all(isinstance(block, EmbeddedResource) for block in blocks)
        assert [typing.cast(EmbeddedResource, block).resource.uri for block in blocks] == [
            f'{API_URL}/contracts/7#attachments[0].content',
            f'{API_URL}/contracts/7#attachments[1].content',
        ]
        assert [item['content'] for item in result.structured_content['attachments']] == [
            '<content[1]: application/pdf>',
            '<content[2]: application/pdf>',
        ]

    async def test_override_wins_over_the_spec(self, mock_upstream):
        """The YAML ``media`` entry replaces what the spec declares for the same path."""
        body = {'photo': self._encode(self._PNG)}
        override = ToolOverride(media={'photo': MediaField(encoding='base64', media_type='image/png')})

        result = await self._call(mock_upstream, body, self._object(photo=self._base64()), override)

        assert isinstance(result.content[1], ImageContent)

    async def test_line_broken_base64_is_decoded(self, mock_upstream):
        """MIME base64 that breaks its lines, as GitHub's contents API sends it, still decodes."""
        encoded = self._encode(self._PNG * 10)
        body = {'photo': '\n'.join(encoded[index : index + 60] for index in range(0, len(encoded), 60))}
        result = await self._call(mock_upstream, body, self._object(photo=self._base64('image/png')))

        (_text, image) = result.content
        assert isinstance(image, ImageContent)
        assert base64.b64decode(image.data) == self._PNG * 10

    async def test_value_that_is_not_base64_is_left_alone(self, mock_upstream):
        """A declared field holding something else, or missing, keeps its value and adds no block."""
        body = {'photo': 'not base64!', 'other': 1}
        response = self._object(photo=self._base64(), missing=self._object(path=self._base64()))

        result = await self._call(mock_upstream, body, response)

        assert len(result.content) == 1
        assert result.structured_content == body

    async def test_blocks_are_numbered_in_payload_order(self, mock_upstream):
        """Blocks follow the response, item by item, and an override field the schema does not declare comes last."""
        png = self._encode(self._PNG)
        body = {'cover': png, 'pages': [{'front': png, 'back': png}, {'front': png, 'back': png}]}
        # The schema declares each page's fields in the other order, so the blocks follow the response, not it.
        page = self._object(back=self._base64('image/png'), front=self._base64('image/png'))
        response = self._object(pages={'type': 'array', 'items': page})
        override = ToolOverride(media={'cover': MediaField(encoding='base64', media_type='image/png')})

        result = await self._call(mock_upstream, body, response, override)

        assert result.structured_content == {
            'cover': '<content[5]: image/png>',
            'pages': [
                {'front': '<content[1]: image/png>', 'back': '<content[2]: image/png>'},
                {'front': '<content[3]: image/png>', 'back': '<content[4]: image/png>'},
            ],
        }

    async def test_response_transform_sees_the_placeholder(self, mock_upstream):
        """Media is promoted from the upstream shape first, so a transform reshapes the placeholder, not the bytes."""
        body = {'signature': self._encode(self._PNG)}
        override = ToolOverride(
            response='{"sig": signature}',
            media={'signature': MediaField(encoding='base64', media_type='image/png')},
        )

        result = await self._call(mock_upstream, body, tool=override)

        assert result.structured_content == {'sig': '<content[1]: image/png>'}
        assert isinstance(result.content[1], ImageContent)
