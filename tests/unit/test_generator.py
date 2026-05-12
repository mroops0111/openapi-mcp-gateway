import inspect
import typing

import httpx
from mcp.server.fastmcp import Context, FastMCP

from openapi_mcp_gateway.generator import ToolGenerator, _sanitize_name
from openapi_mcp_gateway.openapi import McpIntegration, OperationInfo, ParameterInfo


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
        return ToolGenerator(mcp=mcp, base_url='https://api.example.com'), mcp

    def test_tool_name_with_slash_sanitized(self):
        """A slash in ``operation_id`` is sanitised before tool registration."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='meta/root',
            method='get',
            path='/',
        )
        generator.register_operations([operation])
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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'get_team')
        await tool.run({'enterprise_team': 'acme'}, context=_stub_context())
        assert captured['path'] == '/teams/acme'


class TestGeneratedSignature:
    """``_generate_tool_function`` builds a signature with required→ctx→optional order and JSON-Schema-driven types."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Fresh generator + FastMCP for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, base_url='https://api.example.com'), mcp

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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
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
        generator.register_operations([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'do_thing')
        assert tool.fn.__annotations__['ids'] == list[int]


class TestToolOverride:
    """``x-mcp-integration.expose.tool`` overrides reach the registered tool."""

    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        """Fresh generator + FastMCP for each test."""
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, base_url='https://api.example.com'), mcp

    def test_name_override_applied(self):
        """``expose.tool.name`` replaces the auto-derived tool name."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='adminListPets',
            method='get',
            path='/admin/pets',
            x_mcp_integration=McpIntegration.model_validate({'expose': {'tool': {'name': 'list_admin_pets'}}}),
        )
        generator.register_operations([operation])
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert 'list_admin_pets' in names
        assert 'admin_list_pets' not in names

    def test_description_override_applied(self):
        """``expose.tool.description`` replaces the OpenAPI description for the LLM."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='adminListPets',
            method='get',
            path='/admin/pets',
            description='Original description from spec.',
            x_mcp_integration=McpIntegration.model_validate(
                {'expose': {'tool': {'description': 'Override description.'}}}
            ),
        )
        generator.register_operations([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'admin_list_pets')
        assert tool.description == 'Override description.'

    def test_override_name_sanitised(self):
        """A user-supplied name still flows through ``_sanitize_name``."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='adminListPets',
            method='get',
            path='/admin/pets',
            x_mcp_integration=McpIntegration.model_validate({'expose': {'tool': {'name': 'list-admin-pets'}}}),
        )
        generator.register_operations([operation])
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert 'list_admin_pets' in names

    def test_falls_back_to_auto_when_no_override(self):
        """Without an override, name and description come from the operation."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='listPets',
            method='get',
            path='/pets',
            description='Auto description.',
        )
        generator.register_operations([operation])
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'list_pets')
        assert tool.description == 'Auto description.'
