import json
import typing

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from openapi_mcp_gateway.exposure import (
    MetaToolGenerator,
    ToolGenerator,
    UpstreamConnection,
    _upstream,
)
from openapi_mcp_gateway.exposure import meta_tool as meta_tool_module
from openapi_mcp_gateway.exposure import tool as tool_module
from openapi_mcp_gateway.exposure._shared import (
    build_input_schema,
)
from openapi_mcp_gateway.openapi import (
    OperationInfo,
    ParameterInfo,
)
from tests.constants import API_URL
from tests.context import stub_context
from tests.operation import register_tool, shaped_operation


class TestStructuredContent:
    """Successful responses populate ``structuredContent`` for object bodies and leave it ``None`` otherwise."""

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Build a fresh generator + MCPServer pair for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

    async def test_dict_body_populates_structured_content(self, mock_upstream):
        """A JSON object response lands in both ``content[0].text`` and ``structuredContent``."""
        mock_upstream(lambda _request: httpx.Response(200, json={'id': 7, 'name': 'rex'}))
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/7')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=stub_context())
        assert result.is_error is False
        assert result.structured_content == {'id': 7, 'name': 'rex'}
        assert json.loads(result.content[0].text) == {'id': 7, 'name': 'rex'}

    async def test_list_body_omits_structured_content(self, mock_upstream):
        """JSON arrays stay text-only since ``structuredContent`` is object-typed in the MCP spec."""
        mock_upstream(lambda _request: httpx.Response(200, json=[{'id': 1}]))
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='list_pets', method='get', path='/pets')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'list_pets')

        result = await tool.run({}, context=stub_context())
        assert result.is_error is False
        assert result.structured_content is None
        assert json.loads(result.content[0].text) == [{'id': 1}]


class TestBodySerialization:
    """Object-shaped body parameters reach the upstream as plain JSON, not pydantic models.

    ``_schema_to_python_type`` turns an object body into a dynamic model for the LLM-facing schema,
    but httpx cannot ``json.dumps`` a model instance, so the value is converted back before the call.
    These tests pin that conversion.
    """

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Build a fresh generator + MCPServer pair for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

    async def test_array_of_objects_body_serialised_to_json(self, mock_upstream):
        """An ``array<object>`` body param is sent as a JSON array of plain dicts."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='create_order',
            method='post',
            path='/orders',
            parameters=[
                ParameterInfo(
                    name='line_items',
                    location='body',
                    required=True,
                    schema={
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'required': ['id'],
                            'properties': {
                                'id': {'type': 'integer', 'description': 'Identifier.'},
                                'name': {'type': 'string', 'description': 'Display name.'},
                            },
                        },
                    },
                ),
            ],
        )
        generator.register([operation])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'create_order')

        result = await tool.run({'line_items': [{'id': 1, 'name': 'rex'}]}, context=stub_context())
        assert result.is_error is False
        assert captured['body'] == {'line_items': [{'id': 1, 'name': 'rex'}]}

    async def test_object_body_omits_unset_optional_fields(self, mock_upstream):
        """An optional nested field the caller omits stays absent rather than serialising as null."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='create_doc',
            method='post',
            path='/docs',
            parameters=[
                ParameterInfo(
                    name='meta',
                    location='body',
                    required=True,
                    schema={
                        'type': 'object',
                        'required': ['title'],
                        'properties': {
                            'title': {'type': 'string', 'description': 'Title.'},
                            'subtitle': {'type': 'string', 'description': 'Optional subtitle.'},
                        },
                    },
                ),
            ],
        )
        generator.register([operation])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'create_doc')

        result = await tool.run({'meta': {'title': 'hello'}}, context=stub_context())
        assert result.is_error is False
        assert captured['body'] == {'meta': {'title': 'hello'}}


class TestErrorResult:
    """Upstream non-2xx returns an ``isError`` result with the parsed error body when JSON-shaped."""

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Build a fresh generator + MCPServer pair for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

    async def test_json_error_body_in_structured_content(self, mock_upstream):
        """A JSON error body populates ``structuredContent`` and the text contains the status line."""
        mock_upstream(
            lambda _request: httpx.Response(
                404,
                json={'message': 'Not Found', 'documentation_url': f'{API_URL}/docs'},
            )
        )
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/9')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=stub_context())
        assert result.is_error is True
        assert result.structured_content == {
            'message': 'Not Found',
            'documentation_url': f'{API_URL}/docs',
        }
        assert '404' in result.content[0].text
        assert 'Not Found' in result.content[0].text

    async def test_text_error_body_no_structured_content(self, mock_upstream):
        """A non-JSON error body leaves ``structuredContent`` as ``None`` and surfaces the text only."""
        mock_upstream(
            lambda _request: httpx.Response(500, text='internal boom', headers={'content-type': 'text/plain'})
        )
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/9')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=stub_context())
        assert result.is_error is True
        assert result.structured_content is None
        assert 'internal boom' in result.content[0].text

    async def test_network_error_wrapped_as_iserror(self, mock_upstream):
        """A transport failure (connect, timeout, DNS) becomes an ``isError`` result with the exception type."""

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('connection refused', request=request)

        mock_upstream(boom)
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/9')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=stub_context())
        assert result.is_error is True
        assert result.structured_content is None
        assert 'ConnectError' in result.content[0].text
        assert 'connection refused' in result.content[0].text


class TestInputSchemaEnforcement:
    """Advertised constraints are enforced before the upstream call, not merely displayed.

    Validation runs against the same schema the tool advertises,
    so what the client is shown is exactly what gets checked.
    That covers the bounds, pattern, and format that the signature types cannot carry.
    """

    def _register_single_param(self, mcp: MCPServer, name: str, param_schema: dict) -> None:
        """Register a one-parameter ``do_thing`` tool carrying ``param_schema``."""
        generator = ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL))
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[ParameterInfo(name=name, location='query', required=True, schema=param_schema)],
        )
        generator.register([operation])

    async def _call(self, mcp: MCPServer, arguments: dict) -> CallToolResult:
        """Invoke ``do_thing`` through an in-memory client and return the tool result."""
        async with Client(mcp) as client:
            return await client.call_tool('do_thing', arguments)

    @pytest.mark.parametrize('generator_class', [ToolGenerator, MetaToolGenerator])
    def test_schema_is_built_once_and_enforced_as_built(self, monkeypatch, generator_class):
        """Each operation's schema is built once, and the validator checks against that very schema."""
        built: list[dict] = []
        enforced: list[dict] = []

        def counting_build(operation: OperationInfo) -> dict:
            built.append(build_input_schema(operation))
            return built[-1]

        validator_class = _upstream.Draft202012Validator

        def capturing_validator(schema: dict, **options: typing.Any) -> typing.Any:
            enforced.append(schema)
            return validator_class(schema, **options)

        monkeypatch.setattr(tool_module, 'build_input_schema', counting_build)
        monkeypatch.setattr(meta_tool_module, 'build_input_schema', counting_build)
        monkeypatch.setattr(_upstream, 'Draft202012Validator', capturing_validator)
        mcp = MCPServer('test')
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[ParameterInfo(name='n', location='query', required=True, schema={'type': 'integer'})],
        )

        generator_class(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation])

        assert len(built) == 1
        assert len(enforced) == 1
        assert enforced[0] is built[0]
        if generator_class is ToolGenerator:
            tool = mcp._tool_manager.get_tool('do_thing')
            assert tool is not None
            assert tool.parameters is built[0]

    async def test_maximum_enforced(self):
        """A value above ``maximum`` is rejected even though its type is correct."""
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'n', {'type': 'integer', 'minimum': 1, 'maximum': 10})
        result = await self._call(mcp, {'n': 99})
        assert result.is_error
        message = result.content[0]
        assert isinstance(message, TextContent)
        assert 'maximum' in message.text
        assert result.structured_content is not None
        error = result.structured_content['errors'][0]
        assert error['path'] == 'n'
        assert error['keyword'] == 'maximum'

    async def test_pattern_enforced(self):
        """A string that fails ``pattern`` is rejected."""
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'code', {'type': 'string', 'pattern': '^[A-Z]{3}$'})
        result = await self._call(mcp, {'code': 'abc'})
        assert result.is_error

    async def test_format_enforced(self):
        """A string that fails ``format`` is rejected once the format checker is on."""
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'email', {'type': 'string', 'format': 'email'})
        result = await self._call(mcp, {'email': 'not-an-email'})
        assert result.is_error

    async def test_valid_input_reaches_upstream(self, mock_upstream):
        """An in-range value passes validation and the upstream call is made."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['hit'] = True
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'n', {'type': 'integer', 'minimum': 1, 'maximum': 10})
        result = await self._call(mcp, {'n': 5})
        assert not result.is_error
        assert captured.get('hit')


class TestSendDefault:
    """An x-mcp default reaches the upstream when the LLM omits it, without a request expression."""

    async def test_default_sent_when_omitted(self, mock_upstream):
        """An x-mcp default reaches the upstream when the LLM does not supply the parameter."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='limit', location='query', required=False, schema={'type': 'integer'})],
            params={'limit': {'default': 10}},
        )
        await register_tool(op).run({}, context=stub_context())
        assert captured['params']['limit'] == '10'

    async def test_llm_value_overrides_default(self, mock_upstream):
        """A value the LLM supplies wins over the x-mcp default."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='limit', location='query', required=False, schema={'type': 'integer'})],
            params={'limit': {'default': 10}},
        )
        await register_tool(op).run({'limit': 99}, context=stub_context())
        assert captured['params']['limit'] == '99'


class TestHeaders:
    """Headers follow the request expression like every other location, and a server can add static ones."""

    async def test_request_alone_decides_a_hidden_default(self, mock_upstream):
        """A hidden header default reaches the upstream only when the request expression carries it."""
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append({'headers': dict(request.headers), 'params': dict(request.url.params)})
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        parameters = [
            ParameterInfo(name='q', location='query', schema={'type': 'string'}),
            ParameterInfo(name='Api-Version', location='header', schema={'type': 'string'}),
        ]
        params = {'Api-Version': {'hidden': True, 'default': '2026-03-11'}}
        for request in ('{"q": $uppercase(q)}', '$merge([$, {"q": $uppercase(q)}])'):
            op = shaped_operation(parameters, params=params, params_strategy='merge', request=request)
            await register_tool(op).run({'q': 'abc'}, context=stub_context())
        dropped, carried = captured
        assert 'api-version' not in dropped['headers']
        assert carried['headers']['api-version'] == '2026-03-11'
        assert carried['params'] == {'q': 'ABC'}

    async def test_request_key_naming_a_header_routes_to_header(self, mock_upstream):
        """A request result key naming a header, by spec or sanitised name, is sent as that header only."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = dict(request.headers)
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={})

        mock_upstream(handler)
        op = shaped_operation(
            [
                ParameterInfo(name='name', location='body', schema={'type': 'string'}),
                ParameterInfo(name='Api-Version', location='header', schema={'type': 'string'}),
            ],
            params={'Api-Version': {'hidden': True, 'default': '2025-01-01'}},
            params_strategy='merge',
            request='$merge([$, {"Api_Version": "2026-03-11"}])',
            method='post',
        )
        await register_tool(op).run({'name': 'x'}, context=stub_context())
        assert captured['headers']['api-version'] == '2026-03-11'
        assert captured['body'] == {'name': 'x'}

    async def test_replace_keeps_a_named_spec_header(self, mock_upstream):
        """Under replace, an entry naming a spec header stays a header, hidden from the surface."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = dict(request.headers)
            captured['path'] = request.url.path
            return httpx.Response(200, json={})

        mock_upstream(handler)
        op = shaped_operation(
            [
                ParameterInfo(name='page_id', location='path', required=True, schema={'type': 'string'}),
                ParameterInfo(name='Api-Version', location='header', schema={'type': 'string'}),
            ],
            params={
                'doc': {'type': 'string', 'enum': ['home'], 'required': True},
                'Api-Version': {'hidden': True, 'default': '2026-03-11'},
            },
            params_strategy='replace',
            request='{"page_id": $lookup({"home": "abc123"}, doc), "Api_Version": Api_Version}',
            path='/pages/{page_id}',
        )
        tool = register_tool(op)
        assert list(tool.parameters['properties']) == ['doc']
        await tool.run({'doc': 'home'}, context=stub_context())
        assert captured['headers']['api-version'] == '2026-03-11'
        assert captured['path'] == '/pages/abc123'

    async def test_server_headers_sit_beneath_per_call_headers(self, mock_upstream):
        """Server headers go out on every call, and a header the call itself sends wins over them."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['headers'] = dict(request.headers)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='Api-Version', location='header', schema={'type': 'string'})],
            params={'Api-Version': {'hidden': True, 'default': '2026-03-11'}},
            params_strategy='merge',
        )
        mcp = MCPServer('test')
        connection = UpstreamConnection(base_url=API_URL, headers={'Api-Version': '2025-01-01', 'X-Team': 'wren'})
        ToolGenerator(mcp=mcp, connection=connection).register([op])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        await tool.run({}, context=stub_context())
        assert captured['headers']['x-team'] == 'wren'
        assert captured['headers']['api-version'] == '2026-03-11'
