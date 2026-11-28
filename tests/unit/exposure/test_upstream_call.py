import functools
import json
import typing

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult, TextContent

from openapi_mcp_gateway.auth.upstream.reauthorization import UpstreamReauthorizer
from openapi_mcp_gateway.exposure import (
    CallProgress,
    MetaToolGenerator,
    ToolGenerator,
    UpstreamConnection,
    UpstreamOperationCall,
    build_input_schema,
)
from openapi_mcp_gateway.exposure.reauthorization import UpstreamSignInPolicy
from openapi_mcp_gateway.openapi import (
    OperationInfo,
    ParameterInfo,
)
from openapi_mcp_gateway.scopes import UpstreamNoGrantSource
from tests.constants import API_URL
from tests.context import stub_context
from tests.operation import register_tool, shaped_operation
from tests.served_tool import ServedTool
from tests.tool_result import text_of


# The one required string query parameter the sign-in wait tests call with.
_REQUIRED_NAME_PARAMETER = ParameterInfo(name='name', location='query', required=True, schema={'type': 'string'})


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
        generator.register([OperationInfo(operation_id='get_product', method='get', path='/products/7')])
        tool = ServedTool(mcp, 'get_product')

        result = await tool.call({})
        assert result.is_error is False
        assert result.structured_content == {'id': 7, 'name': 'rex'}
        assert json.loads(text_of(result)) == {'id': 7, 'name': 'rex'}

    async def test_list_body_omits_structured_content(self, mock_upstream):
        """JSON arrays stay text-only since ``structuredContent`` is object-typed in the MCP spec."""
        mock_upstream(lambda _request: httpx.Response(200, json=[{'id': 1}]))
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='list_products', method='get', path='/products')])
        tool = ServedTool(mcp, 'list_products')

        result = await tool.call({})
        assert result.is_error is False
        assert result.structured_content is None
        assert json.loads(text_of(result)) == [{'id': 1}]


class TestBodySerialization:
    """Object-shaped body parameters reach the upstream as plain JSON, not pydantic models.

    ``schema_to_python_type`` turns an object body into a dynamic model for the LLM-facing schema,
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
        tool = ServedTool(mcp, 'create_order')

        result = await tool.call({'line_items': [{'id': 1, 'name': 'rex'}]})
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
        tool = ServedTool(mcp, 'create_doc')

        result = await tool.call({'meta': {'title': 'hello'}})
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
        generator.register([OperationInfo(operation_id='get_product', method='get', path='/products/9')])
        tool = ServedTool(mcp, 'get_product')

        result = await tool.call({})
        assert result.is_error is True
        assert result.structured_content == {
            'message': 'Not Found',
            'documentation_url': f'{API_URL}/docs',
        }
        assert '404' in text_of(result)
        assert 'Not Found' in text_of(result)

    async def test_a_json_suffixed_error_body_in_structured_content(self, mock_upstream):
        """A problem detail (RFC 9457) is JSON by its ``+json`` suffix."""
        problem = {'type': 'about:blank', 'title': 'Unprocessable', 'status': 422}
        mock_upstream(
            lambda _request: httpx.Response(
                422, content=json.dumps(problem), headers={'content-type': 'application/problem+json'}
            )
        )
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_product', method='get', path='/products/9')])

        result = await ServedTool(mcp, 'get_product').call({})

        assert result.is_error is True
        assert result.structured_content == problem

    async def test_text_error_body_no_structured_content(self, mock_upstream):
        """A non-JSON error body leaves ``structuredContent`` as ``None`` and surfaces the text only."""
        mock_upstream(
            lambda _request: httpx.Response(500, text='internal boom', headers={'content-type': 'text/plain'})
        )
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_product', method='get', path='/products/9')])
        tool = ServedTool(mcp, 'get_product')

        result = await tool.call({})
        assert result.is_error is True
        assert result.structured_content is None
        assert 'internal boom' in text_of(result)

    async def test_network_error_wrapped_as_iserror(self, mock_upstream):
        """A transport failure (connect, timeout, DNS) becomes an ``isError`` result with the exception type."""

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('connection refused', request=request)

        mock_upstream(boom)
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_product', method='get', path='/products/9')])
        tool = ServedTool(mcp, 'get_product')

        result = await tool.call({})
        assert result.is_error is True
        assert result.structured_content is None
        assert 'ConnectError' in text_of(result)
        assert 'connection refused' in text_of(result)


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

    @pytest.mark.parametrize(
        'generator_class',
        [
            pytest.param(ToolGenerator, id='ToolGenerator'),
            pytest.param(
                functools.partial(MetaToolGenerator, grant_source=UpstreamNoGrantSource()), id='MetaToolGenerator'
            ),
        ],
    )
    async def test_schema_is_built_once_and_enforced_as_built(self, generator_class):
        """The schema a client is shown is the one built from the operation, and every call is checked against it.

        ``maximum`` sits only in that schema, not in the Python signature, so a refusal for it comes from the schema.
        """
        mcp = MCPServer('test')
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(name='n', location='query', required=True, schema={'type': 'integer', 'maximum': 10})
            ],
        )

        generator_class(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)).register([operation])

        if generator_class is ToolGenerator:
            shown = await ServedTool(mcp, 'do_thing').input_schema()
            result = await ServedTool(mcp, 'do_thing').call({'n': 99})
        else:
            described = await ServedTool(mcp, 'get_operation').call({'name': 'do_thing'})
            assert described.structured_content is not None
            shown = described.structured_content['input_schema']
            result = await ServedTool(mcp, 'call_operation').call({'name': 'do_thing', 'arguments': {'n': 99}})
        assert shown == build_input_schema(operation)
        assert result.is_error
        assert result.structured_content is not None
        assert result.structured_content['errors'][0]['keyword'] == 'maximum'

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

    @pytest.mark.parametrize('value', [41.8826, -87.9065, 43.0389, 7])
    async def test_a_decimal_multiple_passes_multiple_of(self, value, upstream_requests):
        """A coordinate written to four decimal places is a multiple of 0.0001, however a float divides it."""
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'latitude', {'type': 'number', 'multipleOf': 0.0001})
        result = await self._call(mcp, {'latitude': value})
        assert not result.is_error
        assert len(upstream_requests) == 1

    @pytest.mark.parametrize('value', [41.88265, 0.00005])
    async def test_a_value_off_the_step_fails_multiple_of(self, value):
        mcp = MCPServer('test')
        self._register_single_param(mcp, 'latitude', {'type': 'number', 'multipleOf': 0.0001})
        result = await self._call(mcp, {'latitude': value})
        assert result.is_error
        assert result.structured_content is not None
        assert result.structured_content['errors'][0]['keyword'] == 'multipleOf'

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
        await register_tool(op).call({})
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
        await register_tool(op).call({'limit': 99})
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
            await register_tool(op).call({'q': 'abc'})
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
        await register_tool(op).call({'name': 'x'})
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
        assert list((await tool.input_schema())['properties']) == ['doc']
        await tool.call({'doc': 'home'})
        assert captured['headers']['api-version'] == '2026-03-11'
        assert captured['path'] == '/pages/abc123'

    async def test_replace_routes_a_spec_header_the_config_left_out(self, upstream_requests):
        """Under replace, a request key naming a spec header the config does not keep still goes to that header."""
        operation = shaped_operation(
            [
                ParameterInfo(name='name', location='body', schema={'type': 'string'}),
                ParameterInfo(name='Api-Version', location='header', schema={'type': 'string'}),
            ],
            params={'version': {'type': 'string', 'required': True}},
            params_strategy='replace',
            request='{"Api-Version": version}',
            method='post',
        )
        await register_tool(operation).call({'version': '2026-03-11'})
        sent = upstream_requests[-1]
        assert sent.headers['api-version'] == '2026-03-11'
        assert sent.content == b''

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
        tool = ServedTool(mcp, 'do_thing')
        await tool.call({})
        assert captured['headers']['x-team'] == 'wren'
        assert captured['headers']['api-version'] == '2026-03-11'


class TestSignInWait:
    """A call settles a sign-in an earlier round offered only once it has a request to send."""

    @staticmethod
    async def _call(operation: OperationInfo, sign_in_policy: UpstreamSignInPolicy, arguments: dict) -> CallToolResult:
        """Call ``operation`` with ``arguments`` under ``sign_in_policy`` and return its tool result."""
        upstream_call = UpstreamOperationCall(
            operation,
            UpstreamConnection(base_url=API_URL),
            input_schema=build_input_schema(operation),
            sign_in_policy=sign_in_policy,
        )
        return typing.cast(CallToolResult, await upstream_call(ctx=stub_context(), **arguments))

    async def test_sendable_request_waits_for_the_answer(self, upstream_requests):
        """A request that will be sent settles the sign-in answer first."""
        sign_in_policy = _RecordingSignInPolicy()
        operation = shaped_operation([_REQUIRED_NAME_PARAMETER])
        result = await self._call(operation, sign_in_policy, {'name': 'x'})
        assert result.is_error is False
        assert sign_in_policy.awaited_answers == 1
        assert len(upstream_requests) == 1

    @pytest.mark.parametrize(
        ('request_expression', 'arguments'),
        [
            pytest.param(None, {'name': 1}, id='validation'),
            pytest.param('{"n": name + 1}', {'name': 'x'}, id='request-transform'),
        ],
    )
    async def test_rejected_arguments_skip_the_wait(self, upstream_requests, request_expression, arguments):
        """Arguments the request cannot be built from return their error without waiting for a sign-in."""
        sign_in_policy = _RecordingSignInPolicy()
        operation = shaped_operation([_REQUIRED_NAME_PARAMETER], request=request_expression)
        result = await self._call(operation, sign_in_policy, arguments)
        assert result.is_error is True
        assert sign_in_policy.awaited_answers == 0
        assert upstream_requests == []


class _RecordingSignInPolicy(UpstreamSignInPolicy):
    """Count each wait for a sign-in answer, and let every request through."""

    def __init__(self) -> None:
        self.awaited_answers = 0

    async def await_answer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, progress: CallProgress
    ) -> CallToolResult | None:
        self.awaited_answers += 1
        return None

    async def ask(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        return None
