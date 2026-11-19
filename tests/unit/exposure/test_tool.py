import inspect

import httpx
from mcp import Client
from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import (
    ToolGenerator,
    UpstreamConnection,
    derive_tool_annotations,
    derive_tool_title,
)
from openapi_mcp_gateway.exposure.tool import _shaping, merge_tool_annotations
from openapi_mcp_gateway.openapi import (
    OperationInfo,
    ParameterInfo,
    ToolOverride,
)
from tests.constants import API_URL
from tests.context import stub_context
from tests.operations import shaped_operation


class TestToolGeneration:
    """In end-to-end tool registration, name sanitisation flows through to the upstream call."""

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Build a fresh generator + MCPServer pair for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

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
        await tool.run({'enterprise_team': 'foo'}, context=stub_context())
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
        await tool.run({'enterprise_team': 'acme'}, context=stub_context())
        assert captured['path'] == '/teams/acme'


class TestGeneratedSignature:
    """``build_tool_function`` builds the MCPServer-facing signature.

    Required params come first, then the injected ``ctx``, then optional params,
    and parameter annotations are driven by the JSON Schema attached to each spec parameter.
    """

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Fresh generator + MCPServer for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

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


class TestDeriveToolTitle:
    """``derive_tool_title`` mirrors OpenAPI ``summary`` and omits the field when absent."""

    def test_summary_becomes_title(self):
        """``summary`` is used as the human-readable display name."""
        operation = OperationInfo(operation_id='listPets', method='get', path='/pets', summary='List all pets')
        assert derive_tool_title(operation) == 'List all pets'

    def test_empty_summary_returns_none(self):
        """An empty ``summary`` returns ``None`` so the field is omitted from the tool."""
        operation = OperationInfo(operation_id='listPets', method='get', path='/pets', summary='')
        assert derive_tool_title(operation) is None


class TestDeriveToolAnnotations:
    """``derive_tool_annotations`` maps HTTP methods to ``readOnly`` / ``idempotent`` / ``destructive`` hints."""

    def _operation(self, method: str, summary: str = '') -> OperationInfo:
        """Build a minimal operation with only the method (and optional ``summary``) varying."""
        return OperationInfo(operation_id='op', method=method, path='/r', summary=summary)

    def test_get_is_read_only_and_idempotent(self):
        """``GET`` is safe and idempotent, never destructive."""
        annotations = derive_tool_annotations(self._operation('get'))
        assert annotations.read_only_hint is True
        assert annotations.idempotent_hint is True
        assert annotations.destructive_hint is None
        assert annotations.open_world_hint is True

    def test_summary_mirrored_to_annotations_title(self):
        """``summary`` is also surfaced on ``ToolAnnotations.title`` for legacy clients."""
        annotations = derive_tool_annotations(self._operation('get', summary='List pets'))
        assert annotations.title == 'List pets'

    def test_empty_summary_omits_annotations_title(self):
        """No ``summary`` means no ``ToolAnnotations.title`` field."""
        annotations = derive_tool_annotations(self._operation('get'))
        assert annotations.title is None

    def test_put_idempotent_not_destructive(self):
        """``PUT`` is idempotent without being read-only or destructive."""
        annotations = derive_tool_annotations(self._operation('put'))
        assert annotations.read_only_hint is None
        assert annotations.idempotent_hint is True
        assert annotations.destructive_hint is None

    def test_patch_idempotent_not_destructive(self):
        """``PATCH`` is idempotent without being destructive."""
        annotations = derive_tool_annotations(self._operation('patch'))
        assert annotations.idempotent_hint is True
        assert annotations.destructive_hint is None

    def test_delete_destructive_and_idempotent(self):
        """``DELETE`` is destructive and idempotent."""
        annotations = derive_tool_annotations(self._operation('delete'))
        assert annotations.destructive_hint is True
        assert annotations.idempotent_hint is True
        assert annotations.read_only_hint is None

    def test_post_neither_idempotent_nor_destructive(self):
        """``POST`` carries no idempotency or destructiveness guarantees."""
        annotations = derive_tool_annotations(self._operation('post'))
        assert annotations.read_only_hint is None
        assert annotations.idempotent_hint is None
        assert annotations.destructive_hint is None
        assert annotations.open_world_hint is True


class TestToolRegistrationMetadata:
    """Registered tools advertise ``title`` and ``annotations`` derived from the spec."""

    def _generator(self) -> tuple[ToolGenerator, MCPServer]:
        """Build a fresh generator + MCPServer pair for each test."""
        mcp = MCPServer('test')
        return ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL)), mcp

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
        assert annotations.read_only_hint is True
        assert annotations.idempotent_hint is True
        assert annotations.open_world_hint is True


class TestFaithfulInputSchema:
    """The advertised tool input schema preserves OpenAPI keywords a Python signature cannot carry.

    Static tools overwrite the signature-derived schema with ``build_input_schema``,
    so ``format``, numeric bounds, ``pattern``, ``enum``, ``default``, composition, and nesting survive to the LLM.
    """

    def _schema_for(self, *parameters: ParameterInfo) -> dict:
        """Register one operation carrying ``parameters`` and return its advertised input schema."""
        mcp = MCPServer('test')
        generator = ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL))
        operation = OperationInfo(operation_id='do_thing', method='post', path='/things', parameters=list(parameters))
        generator.register([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        return tool.parameters

    def test_string_format_preserved(self):
        """A ``format`` keyword survives into the advertised schema."""
        schema = self._schema_for(
            ParameterInfo(name='email', location='query', required=True, schema={'type': 'string', 'format': 'email'})
        )
        assert schema['properties']['email']['format'] == 'email'

    def test_numeric_bounds_preserved(self):
        """``minimum`` and ``maximum`` survive into the advertised schema."""
        schema = self._schema_for(
            ParameterInfo(
                name='limit', location='query', required=False, schema={'type': 'integer', 'minimum': 1, 'maximum': 100}
            )
        )
        prop = schema['properties']['limit']
        assert prop['minimum'] == 1
        assert prop['maximum'] == 100

    def test_pattern_preserved(self):
        """A ``pattern`` keyword survives into the advertised schema."""
        schema = self._schema_for(
            ParameterInfo(
                name='code', location='query', required=True, schema={'type': 'string', 'pattern': '^[A-Z]{3}$'}
            )
        )
        assert schema['properties']['code']['pattern'] == '^[A-Z]{3}$'

    def test_enum_preserved(self):
        """An ``enum`` survives with its full value list."""
        schema = self._schema_for(
            ParameterInfo(
                name='status', location='query', required=True, schema={'type': 'string', 'enum': ['open', 'closed']}
            )
        )
        assert schema['properties']['status']['enum'] == ['open', 'closed']

    def test_default_preserved(self):
        """A ``default`` value survives into the advertised schema."""
        schema = self._schema_for(
            ParameterInfo(name='page', location='query', required=False, schema={'type': 'integer', 'default': 1})
        )
        assert schema['properties']['page']['default'] == 1

    def test_nested_object_body_preserved(self):
        """A nested object body keeps its properties, nested constraints, and required list."""
        body = {
            'type': 'object',
            'properties': {'street': {'type': 'string'}, 'zip': {'type': 'string', 'pattern': r'\d{5}'}},
            'required': ['street'],
        }
        schema = self._schema_for(ParameterInfo(name='address', location='body', required=True, schema=body))
        prop = schema['properties']['address']
        assert prop['type'] == 'object'
        assert prop['properties']['zip']['pattern'] == r'\d{5}'
        assert prop['required'] == ['street']

    def test_array_items_preserved(self):
        """An array item schema keeps its keywords."""
        schema = self._schema_for(
            ParameterInfo(
                name='ids',
                location='query',
                required=False,
                schema={'type': 'array', 'items': {'type': 'string', 'format': 'uuid'}},
            )
        )
        prop = schema['properties']['ids']
        assert prop['type'] == 'array'
        assert prop['items']['format'] == 'uuid'

    def test_composition_oneof_preserved(self):
        """A ``oneOf`` composition survives verbatim."""
        schema = self._schema_for(
            ParameterInfo(
                name='value',
                location='query',
                required=True,
                schema={'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
            )
        )
        assert schema['properties']['value']['oneOf'] == [{'type': 'string'}, {'type': 'integer'}]

    def test_required_and_optional_partition(self):
        """Required parameters land in ``required``, optional ones stay out of it."""
        schema = self._schema_for(
            ParameterInfo(name='must', location='query', required=True, schema={'type': 'string'}),
            ParameterInfo(name='maybe', location='query', required=False, schema={'type': 'string'}),
        )
        assert schema['required'] == ['must']
        assert {'must', 'maybe'} <= set(schema['properties'])

    def test_param_description_merged(self):
        """A parameter-level description is carried onto its property schema."""
        schema = self._schema_for(
            ParameterInfo(
                name='q', location='query', required=True, description='search query', schema={'type': 'string'}
            )
        )
        assert schema['properties']['q']['description'] == 'search query'

    async def test_faithful_schema_visible_to_client(self):
        """The faithful schema reaches the client's tools/list, not just the internal Tool object."""
        mcp = MCPServer('test')
        generator = ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL))
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(
                    name='email', location='query', required=True, schema={'type': 'string', 'format': 'email'}
                )
            ],
        )
        generator.register([operation])
        async with Client(mcp) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'do_thing')
        assert tool.input_schema['properties']['email']['format'] == 'email'

    async def test_validation_still_rejects_wrong_type(self):
        """Faithful display does not weaken validation, so a type mismatch is still rejected."""
        mcp = MCPServer('test')
        generator = ToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL))
        operation = OperationInfo(
            operation_id='do_thing',
            method='get',
            path='/things',
            parameters=[ParameterInfo(name='count', location='query', required=True, schema={'type': 'integer'})],
        )
        generator.register([operation])
        async with Client(mcp) as client:
            result = await client.call_tool('do_thing', {'count': 'not-an-int'})
        assert result.is_error


class TestShaping:
    """``_shaping`` reports which reshaping took effect, for a caller rather than for a column."""

    def test_no_override_reshapes_nothing(self):
        assert _shaping(None) is None

    def test_each_kind_of_reshaping_is_named(self):
        override = ToolOverride.model_validate(
            {
                'params_strategy': 'replace',
                'params': {'x': {'type': 'string'}},
                'request': '{}',
                'response': 'r',
                'media': {'photo': {'encoding': 'base64'}},
            }
        )
        assert _shaping(override) == {'params': 'replace', 'request': True, 'response': True, 'media': True}

    def test_a_rename_alone_does_not_reshape_the_call(self):
        """The name changes, the request and the schema do not."""
        assert _shaping(ToolOverride(name='foo')) is None

    def test_a_strategy_without_params_takes_no_effect(self):
        """The config names a strategy, but with nothing to apply it to it changes nothing.

        Reporting the declaration here would describe the config rather than the result.
        """
        assert _shaping(ToolOverride.model_validate({'params_strategy': 'merge'})) is None


class TestMergeToolAnnotations:
    """Author annotation overrides merge over the method-derived defaults, explicit winning."""

    def test_none_override_equals_derived(self):
        """No override returns exactly the method-derived annotations."""
        op = shaped_operation([], method='get')
        assert merge_tool_annotations(op, None) == derive_tool_annotations(op)

    def test_post_can_be_marked_read_only(self):
        """A POST can be flipped read-only while inheriting the other method-derived hints."""
        op = shaped_operation([], method='post')
        merged = merge_tool_annotations(op, {'readOnlyHint': True})
        assert merged.read_only_hint is True
        assert merged.open_world_hint is True

    def test_get_can_be_downgraded(self):
        """An explicit ``readOnlyHint: false`` wins over the GET default."""
        op = shaped_operation([], method='get')
        assert merge_tool_annotations(op, {'readOnlyHint': False}).read_only_hint is False

    def test_snake_case_key_accepted(self):
        """Override keys may be snake_case as well as camelCase."""
        op = shaped_operation([], method='post')
        assert merge_tool_annotations(op, {'read_only_hint': True}).read_only_hint is True
