import inspect
import json
import typing

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP

from openapi_mcp_gateway.exposure import (
    MetaToolGenerator,
    ToolGenerator,
    UpstreamBinding,
    _sanitize_name,
    derive_annotations,
    derive_title,
)
from openapi_mcp_gateway.openapi import OperationInfo, ParameterInfo


class _StubContext:
    """No-op MCP context used when invoking generated tools in tests."""

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context() -> Context:
    """Return a ``Context``-typed stub suitable for tool invocation."""
    return typing.cast(Context, _StubContext())


class TestSanitizeName:
    """Conversion of arbitrary OpenAPI names into valid Python identifiers."""

    def test_slash_to_underscore(self):
        """Slashes become underscores."""
        assert _sanitize_name('meta/root') == 'meta_root'

    def test_dash_to_underscore(self):
        """Dashes become underscores."""
        assert _sanitize_name('enterprise-team') == 'enterprise_team'

    def test_dot_to_underscore(self):
        """Dots become underscores."""
        assert _sanitize_name('foo.bar') == 'foo_bar'

    def test_leading_digit_prefixed(self):
        """A leading digit gets prefixed with ``_`` to form a valid identifier."""
        assert _sanitize_name('1st_param') == '_1st_param'

    def test_already_valid_unchanged(self):
        """A name that is already a valid identifier is unchanged."""
        assert _sanitize_name('valid_name') == 'valid_name'

    def test_python_keyword_suffixed(self):
        """Python keywords get a trailing underscore to avoid syntax errors."""
        assert _sanitize_name('async') == 'async_'
        assert _sanitize_name('class') == 'class_'
        assert _sanitize_name('from') == 'from_'


class TestToolGeneration:
    """End-to-end tool registration: name sanitisation flows through to the upstream call."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Build a fresh generator + FastMCP pair for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp

    def test_tool_name_with_slash_sanitized(self):
        """A slash in ``operation_id`` is sanitised before tool registration."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='meta/root',
            method='get',
            path='/',
        )
        generator.register([operation])
        assert 'meta_root' in {tool.name for tool in mcp._tool_manager.list_tools()}

    def test_param_name_with_dash_sanitized(self):
        """A dashed parameter name is exposed via its sanitised identifier on the tool signature."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='get_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(name='enterprise-team', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'get_thing')
        signature = inspect.signature(tool.fn)
        assert 'enterprise_team' in signature.parameters
        assert 'enterprise-team' not in signature.parameters

    async def test_dashed_param_sent_with_original_name(self, mock_upstream):
        """Sanitised python name maps back to the original API name when calling upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)

        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='get_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(name='enterprise-team', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'get_thing')
        await tool.run({'enterprise_team': 'foo'}, context=_stub_context())
        assert 'enterprise-team=foo' in captured['url']

    async def test_path_param_with_dash_substituted(self, mock_upstream):
        """A dashed path parameter is substituted using the original name in the URL."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)

        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[
                ParameterInfo(name='enterprise-team', location='path', required=True, schema={'type': 'string'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'get_team')
        await tool.run({'enterprise_team': 'acme'}, context=_stub_context())
        assert captured['path'] == '/teams/acme'


class TestGeneratedSignature:
    """``build_tool_function`` builds the FastMCP-facing signature.

    Required params come first, then the injected ``ctx``, then optional params,
    and parameter annotations are driven by the JSON Schema attached to each spec parameter.
    """

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Fresh generator + FastMCP for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp

    def test_param_order_required_then_ctx_then_optional(self):
        """Required params come first, then ``ctx``, then optional params with default ``None``."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/thing',
            parameters=[
                ParameterInfo(name='alpha', location='query', required=False, schema={'type': 'string'}),
                ParameterInfo(name='beta', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        names = list(inspect.signature(tool.fn).parameters)
        assert names == ['beta', 'ctx', 'alpha']

    def test_optional_param_has_none_default(self):
        """Optional parameters carry ``default=None`` on the signature."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/thing',
            parameters=[
                ParameterInfo(name='maybe', location='query', required=False, schema={'type': 'integer'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        signature = inspect.signature(tool.fn)
        assert signature.parameters['maybe'].default is None

    def test_required_param_has_no_default(self):
        """Required parameters use ``inspect.Parameter.empty`` as their default."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/thing',
            parameters=[
                ParameterInfo(name='must', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        signature = inspect.signature(tool.fn)
        assert signature.parameters['must'].default is inspect.Parameter.empty

    def test_schema_types_mapped_to_python_types(self):
        """JSON-Schema ``string`` / ``integer`` / ``number`` / ``boolean`` map to native Python types."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/thing',
            parameters=[
                ParameterInfo(name='s', location='query', required=True, schema={'type': 'string'}),
                ParameterInfo(name='i', location='query', required=True, schema={'type': 'integer'}),
                ParameterInfo(name='f', location='query', required=True, schema={'type': 'number'}),
                ParameterInfo(name='b', location='query', required=True, schema={'type': 'boolean'}),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        annotations = tool.fn.__annotations__
        assert annotations['s'] is str
        assert annotations['i'] is int
        assert annotations['f'] is float
        assert annotations['b'] is bool

    def test_array_schema_maps_to_typed_list(self):
        """Array schemas annotate as ``list[<item_type>]``."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/thing',
            parameters=[
                ParameterInfo(
                    name='ids',
                    location='query',
                    required=True,
                    schema={'type': 'array', 'items': {'type': 'integer'}},
                ),
            ],
        )
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        assert tool.fn.__annotations__['ids'] == list[int]


def _meta_generator() -> tuple[MetaToolGenerator, FastMCP]:
    """Build a fresh MetaToolGenerator + FastMCP pair for each test."""
    mcp = FastMCP('test')
    return MetaToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp


def _ops() -> list[OperationInfo]:
    """Two operations covering required path param + optional query param."""
    return [
        OperationInfo(
            operation_id='listPets',
            method='get',
            path='/pets',
            description='List all pets.',
            parameters=[
                ParameterInfo(name='limit', location='query', required=False, schema={'type': 'integer'}),
            ],
        ),
        OperationInfo(
            operation_id='getPetById',
            method='get',
            path='/pets/{petId}',
            summary='Get one pet.',
            parameters=[
                ParameterInfo(name='petId', location='path', required=True, schema={'type': 'integer'}),
            ],
        ),
    ]


class TestMetaToolRegistration:
    """``MetaToolGenerator.register`` produces exactly three meta-tools regardless of N operations."""

    def test_exactly_three_tools_registered(self):
        """Three operations in, three meta-tools out: list / get / call."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert names == {'list_operations', 'get_operation', 'call_operation'}

    def test_empty_ops_still_registers_three(self):
        """No operations still produces three meta-tools (callable on an empty registry)."""
        generator, mcp = _meta_generator()
        generator.register([])
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert names == {'list_operations', 'get_operation', 'call_operation'}


class TestListOperations:
    """``list_operations`` returns ``{name, description}`` for every indexed operation."""

    async def test_returns_name_and_description_only(self):
        """The list payload carries ``name`` and ``description`` per entry, nothing else."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'list_operations')
        payload = json.loads(await tool.fn(ctx=_stub_context()))
        assert payload == [
            {'name': 'list_pets', 'description': 'List all pets.'},
            {'name': 'get_pet_by_id', 'description': 'Get one pet.'},
        ]

    async def test_falls_back_to_method_path_when_no_text(self):
        """An op without description or summary falls back to ``METHOD /path``."""
        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(operation_id='ping', method='get', path='/ping'),
            ]
        )
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'list_operations')
        payload = json.loads(await tool.fn(ctx=_stub_context()))
        assert payload == [{'name': 'ping', 'description': 'GET /ping'}]


class TestGetOperation:
    """``get_operation`` returns the input schema for one operation; unknown names raise."""

    async def test_known_returns_full_schema(self):
        """A known name yields ``{name, description, input_schema}`` with the JSON Schema for inputs."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_operation')
        payload = json.loads(await tool.fn(name='get_pet_by_id', ctx=_stub_context()))
        assert payload['name'] == 'get_pet_by_id'
        assert payload['description'] == 'Get one pet.'
        schema = payload['input_schema']
        assert schema['type'] == 'object'
        assert schema['properties']['petId']['type'] == 'integer'
        assert schema['required'] == ['petId']

    async def test_unknown_raises(self):
        """An unknown name raises ``ValueError`` so FastMCP surfaces it as a tool error."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_operation')
        with pytest.raises(ValueError, match='Unknown operation'):
            await tool.fn(name='no_such_op', ctx=_stub_context())

    async def test_no_required_key_when_all_optional(self):
        """When no parameter is required, the schema omits the ``required`` array entirely."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_operation')
        payload = json.loads(await tool.fn(name='list_pets', ctx=_stub_context()))
        assert 'required' not in payload['input_schema']

    async def test_param_description_propagates(self):
        """``ParameterInfo.description`` lands on the JSON Schema property."""
        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(
                    operation_id='listPets',
                    method='get',
                    path='/pets',
                    parameters=[
                        ParameterInfo(
                            name='limit',
                            location='query',
                            required=False,
                            description='Max items.',
                            schema={'type': 'integer'},
                        ),
                    ],
                )
            ]
        )
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_operation')
        payload = json.loads(await tool.fn(name='list_pets', ctx=_stub_context()))
        assert payload['input_schema']['properties']['limit']['description'] == 'Max items.'


class TestCallOperation:
    """``call_operation`` dispatches to the underlying HTTP callable; unknown names raise."""

    async def test_dispatches_to_upstream(self, mock_upstream):
        """Calling a known op hits the upstream with the resolved path and query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'id': 7, 'name': 'rex'})

        mock_upstream(handler)

        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'call_operation')

        result = await tool.fn(name='get_pet_by_id', arguments={'petId': 7}, ctx=_stub_context())
        assert captured['method'] == 'GET'
        assert captured['url'].endswith('/pets/7')
        assert result.isError is False
        assert result.structuredContent == {'id': 7, 'name': 'rex'}
        assert json.loads(result.content[0].text) == {'id': 7, 'name': 'rex'}

    async def test_unknown_raises(self):
        """An unknown name raises ``ValueError`` without contacting upstream."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'call_operation')
        with pytest.raises(ValueError, match='Unknown operation'):
            await tool.fn(name='no_such_op', arguments={}, ctx=_stub_context())

    async def test_sanitised_arg_name_reaches_upstream(self, mock_upstream):
        """Dashed query params are exposed with sanitised keys; upstream sees the original name."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)

        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(
                    operation_id='getThing',
                    method='get',
                    path='/things',
                    parameters=[
                        ParameterInfo(
                            name='enterprise-team',
                            location='query',
                            required=True,
                            schema={'type': 'string'},
                        ),
                    ],
                )
            ]
        )
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'call_operation')
        await tool.fn(name='get_thing', arguments={'enterprise_team': 'acme'}, ctx=_stub_context())
        assert 'enterprise-team=acme' in captured['url']


class TestDeriveTitle:
    """``derive_title`` mirrors OpenAPI ``summary`` and omits the field when absent."""

    def test_summary_becomes_title(self):
        """``summary`` is used as the human-readable display name."""
        operation = OperationInfo(operation_id='listPets', method='get', path='/pets', summary='List all pets')
        assert derive_title(operation) == 'List all pets'

    def test_empty_summary_returns_none(self):
        """An empty ``summary`` returns ``None`` so the field is omitted from the tool."""
        operation = OperationInfo(operation_id='listPets', method='get', path='/pets', summary='')
        assert derive_title(operation) is None


class TestDeriveAnnotations:
    """``derive_annotations`` maps HTTP methods to ``readOnly`` / ``idempotent`` / ``destructive`` hints."""

    def _operation(self, method: str, summary: str = '') -> OperationInfo:
        """Build a minimal operation with only the method (and optional ``summary``) varying."""
        return OperationInfo(operation_id='op', method=method, path='/r', summary=summary)

    def test_get_is_read_only_and_idempotent(self):
        """``GET`` is safe and idempotent; never destructive."""
        annotations = derive_annotations(self._operation('get'))
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.destructiveHint is None
        assert annotations.openWorldHint is True

    def test_summary_mirrored_to_annotations_title(self):
        """``summary`` is also surfaced on ``ToolAnnotations.title`` for legacy clients."""
        annotations = derive_annotations(self._operation('get', summary='List pets'))
        assert annotations.title == 'List pets'

    def test_empty_summary_omits_annotations_title(self):
        """No ``summary`` means no ``ToolAnnotations.title`` field."""
        annotations = derive_annotations(self._operation('get'))
        assert annotations.title is None

    def test_put_idempotent_not_destructive(self):
        """``PUT`` is idempotent without being read-only or destructive."""
        annotations = derive_annotations(self._operation('put'))
        assert annotations.readOnlyHint is None
        assert annotations.idempotentHint is True
        assert annotations.destructiveHint is None

    def test_patch_idempotent_not_destructive(self):
        """``PATCH`` is idempotent without being destructive."""
        annotations = derive_annotations(self._operation('patch'))
        assert annotations.idempotentHint is True
        assert annotations.destructiveHint is None

    def test_delete_destructive_and_idempotent(self):
        """``DELETE`` is destructive and idempotent."""
        annotations = derive_annotations(self._operation('delete'))
        assert annotations.destructiveHint is True
        assert annotations.idempotentHint is True
        assert annotations.readOnlyHint is None

    def test_post_neither_idempotent_nor_destructive(self):
        """``POST`` carries no idempotency or destructiveness guarantees."""
        annotations = derive_annotations(self._operation('post'))
        assert annotations.readOnlyHint is None
        assert annotations.idempotentHint is None
        assert annotations.destructiveHint is None
        assert annotations.openWorldHint is True


class TestToolRegistrationMetadata:
    """Registered tools advertise ``title`` and ``annotations`` derived from the spec."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Build a fresh generator + FastMCP pair for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp

    def test_tool_advertises_title_and_annotations(self):
        """Registered tool carries ``title`` from ``summary`` and ``annotations`` from method."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='list_pets',
            method='get',
            path='/pets',
            summary='List pets',
        )
        generator.register([operation])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'list_pets')
        annotations = tool.annotations
        assert annotations is not None
        assert tool.title == 'List pets'
        assert annotations.title == 'List pets'
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is True


class TestStructuredContent:
    """Successful responses populate ``structuredContent`` for object bodies and leave it ``None`` otherwise."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Build a fresh generator + FastMCP pair for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp

    async def test_dict_body_populates_structured_content(self, mock_upstream):
        """A JSON object response lands in both ``content[0].text`` and ``structuredContent``."""
        mock_upstream(lambda _request: httpx.Response(200, json={'id': 7, 'name': 'rex'}))
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/7')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=_stub_context())
        assert result.isError is False
        assert result.structuredContent == {'id': 7, 'name': 'rex'}
        assert json.loads(result.content[0].text) == {'id': 7, 'name': 'rex'}

    async def test_list_body_omits_structured_content(self, mock_upstream):
        """JSON arrays stay text-only since ``structuredContent`` is object-typed in the MCP spec."""
        mock_upstream(lambda _request: httpx.Response(200, json=[{'id': 1}]))
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='list_pets', method='get', path='/pets')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'list_pets')

        result = await tool.run({}, context=_stub_context())
        assert result.isError is False
        assert result.structuredContent is None
        assert json.loads(result.content[0].text) == [{'id': 1}]


class TestErrorResult:
    """Upstream non-2xx returns an ``isError`` result with the parsed error body when JSON-shaped."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Build a fresh generator + FastMCP pair for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, binding=UpstreamBinding(base_url='https://api.example.com')), mcp

    async def test_json_error_body_in_structured_content(self, mock_upstream):
        """A JSON error body populates ``structuredContent`` and the text contains the status line."""
        mock_upstream(
            lambda _request: httpx.Response(
                404,
                json={'message': 'Not Found', 'documentation_url': 'https://api.example.com/docs'},
            )
        )
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/9')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=_stub_context())
        assert result.isError is True
        assert result.structuredContent == {
            'message': 'Not Found',
            'documentation_url': 'https://api.example.com/docs',
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

        result = await tool.run({}, context=_stub_context())
        assert result.isError is True
        assert result.structuredContent is None
        assert 'internal boom' in result.content[0].text

    async def test_network_error_wrapped_as_iserror(self, mock_upstream):
        """A transport failure (connect, timeout, DNS) becomes an ``isError`` result with the exception type."""

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('connection refused', request=request)

        mock_upstream(boom)
        generator, mcp = self._generator()
        generator.register([OperationInfo(operation_id='get_pet', method='get', path='/pets/9')])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_pet')

        result = await tool.run({}, context=_stub_context())
        assert result.isError is True
        assert result.structuredContent is None
        assert 'ConnectError' in result.content[0].text
        assert 'connection refused' in result.content[0].text
