import inspect
import typing

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from openapi_mcp_gateway.exposure import (
    ResourceGenerator,
    ToolGenerator,
    UpstreamBinding,
    build_resource_read_function,
    derive_description,
    derive_name,
    derive_resource_mime_type,
    derive_resource_uri,
)
from openapi_mcp_gateway.gateway import (
    _partition_operations,
    _validate_resource_eligibility,
)
from openapi_mcp_gateway.openapi import (
    McpIntegration,
    OperationInfo,
    ParameterInfo,
    ResourceOverride,
    ToolOverride,
)


class _StubContext:
    """No-op MCP context for direct invocation of template-resource read functions in tests."""

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context() -> Context:
    """Return a ``Context``-typed stub suitable for invoking resource read functions directly."""
    return typing.cast(Context, _StubContext())


def _expose_resource(**kwargs) -> McpIntegration:
    """Build an ``x-mcp-integration`` payload that opts an operation into resource exposure only."""
    return McpIntegration(resource=ResourceOverride(**kwargs))


def _expose_tool_and_resource() -> McpIntegration:
    """Build an ``x-mcp-integration`` payload that opts an operation into both tool and resource exposure."""
    return McpIntegration(tool=ToolOverride(), resource=ResourceOverride())


def _resource_override_name(operation: OperationInfo) -> str | None:
    """Pull ``resource.name`` off ``operation`` (or ``None``), for passing to :func:`derive_name`."""
    override = operation.x_mcp_integration.resource
    return override.name if override else None


def _resource_override_description(operation: OperationInfo) -> str | None:
    """Pull ``resource.description`` off ``operation`` (or ``None``),
    for passing to :func:`derive_description`.
    """
    override = operation.x_mcp_integration.resource
    return override.description if override else None


class TestResourceUriDerivation:
    """``derive_resource_uri`` produces MCPServer-compatible URI templates."""

    def test_no_path_params(self):
        """A path with no params yields a concrete URI under the server scheme."""
        operation = OperationInfo(
            operation_id='list_pets',
            method='get',
            path='/pets',
            x_mcp_integration=_expose_resource(),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://pets'

    def test_single_path_param_preserved(self):
        """An identifier-safe path placeholder passes through unchanged."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://pets/{petId}'

    def test_dashed_path_param_sanitized(self):
        """A dashed placeholder is rewritten to a Python identifier so MCPServer's regex matches."""
        operation = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[ParameterInfo(name='enterprise-team', location='path', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        assert derive_resource_uri('github', operation) == 'github://teams/{enterprise_team}'

    def test_uri_template_override_used_verbatim(self):
        """An explicit ``uri_template`` overrides the auto-derived URI exactly."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(uri_template='petstore://v2/pets/{petId}'),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://v2/pets/{petId}'


class TestDeriveNameWithResourceOverride:
    """``derive_name`` honors the resource-side override and falls back to the underscored ``operationId``."""

    def test_default_from_operation_id(self):
        """With no override, the name is the underscored, sanitised ``operationId``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(),
        )
        assert derive_name(operation, _resource_override_name(operation)) == 'get_pet'

    def test_override_wins(self):
        """An explicit ``name`` override on the resource opt-in replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(name='pet'),
        )
        assert derive_name(operation, _resource_override_name(operation)) == 'pet'


class TestDeriveDescriptionWithResourceOverride:
    """``derive_description`` falls back through description / summary / ``METHOD /path`` when no override is set."""

    def test_default_from_description(self):
        """The OpenAPI ``description`` is used when set and the override is absent."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            description='Fetch one pet.',
            x_mcp_integration=_expose_resource(),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'Fetch one pet.'

    def test_fallback_to_method_path(self):
        """Without description or summary, fall back to ``METHOD /path``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'GET /pets/{petId}'

    def test_override_wins(self):
        """An explicit ``description`` override on the resource opt-in replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            description='Fetch one pet.',
            x_mcp_integration=_expose_resource(description='Custom blurb.'),
        )
        assert derive_description(operation, _resource_override_description(operation)) == 'Custom blurb.'


class TestResourceMimeType:
    """``derive_resource_mime_type`` defaults to JSON and honors overrides."""

    def test_default_is_json(self):
        """Default mime type is ``application/json``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(),
        )
        assert derive_resource_mime_type(operation) == 'application/json'

    def test_override_wins(self):
        """An explicit ``mime_type`` override replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(mime_type='text/plain'),
        )
        assert derive_resource_mime_type(operation) == 'text/plain'


class TestResourceReadFunctionSignature:
    """Signature shape drives MCPServer's concrete-vs-template registration.

    - GET with path params: signature is ``(p1, ..., ctx: Context)`` -> template registration,
      and MCPServer injects a real ``Context`` at call time.
    - GET without path params: empty signature -> concrete registration,
      and ``_NullContext`` is used internally.
    """

    def test_path_params_signature_includes_ctx(self):
        """Path-param signature is ``[path_params..., ctx]``; query / body parameters are dropped."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[
                ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'}),
                ParameterInfo(name='verbose', location='query', required=False, schema={'type': 'boolean'}),
            ],
            x_mcp_integration=_expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        names = list(inspect.signature(fn).parameters)
        assert names == ['petId', 'ctx']

    def test_no_path_params_yields_empty_signature(self):
        """A GET with no path params has zero signature parameters, qualifying it as a concrete resource."""
        operation = OperationInfo(
            operation_id='get_inventory',
            method='get',
            path='/store/inventory',
            x_mcp_integration=_expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        assert list(inspect.signature(fn).parameters) == []

    def test_dashed_path_param_sanitised(self):
        """Sanitised identifier appears on the signature, matching the URI template's placeholder."""
        operation = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[
                ParameterInfo(name='enterprise-team', location='path', required=True, schema={'type': 'string'}),
            ],
            x_mcp_integration=_expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        assert 'enterprise_team' in inspect.signature(fn).parameters


class TestResourceGeneration:
    """End-to-end resource registration on an MCPServer instance."""

    def _generator(self) -> tuple[ResourceGenerator, MCPServer]:
        mcp = MCPServer('test')
        return (
            ResourceGenerator(
                mcp=mcp,
                binding=UpstreamBinding(base_url='https://api.example.com'),
                server_name='petstore',
            ),
            mcp,
        )

    async def test_resource_template_registered(self):
        """A GET with a path param registers as a ``ResourceTemplate`` under the derived URI."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            description='Fetch one pet.',
            parameters=[ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'})],
            x_mcp_integration=_expose_resource(),
        )
        generator.register([operation])
        templates = await mcp.list_resource_templates()
        uris = {t.uri_template for t in templates}
        assert 'petstore://pets/{petId}' in uris
        template = next(t for t in templates if t.uri_template == 'petstore://pets/{petId}')
        assert template.description == 'Fetch one pet.'
        assert template.mime_type == 'application/json'

    async def test_no_path_param_registered_as_concrete_resource(self):
        """A GET with no path params surfaces under ``resources/list`` as a concrete resource."""
        generator, mcp = self._generator()
        operation = OperationInfo(
            operation_id='listPets',
            method='get',
            path='/pets',
            x_mcp_integration=_expose_resource(),
        )
        generator.register([operation])
        resources = await mcp.list_resources()
        uris = {str(r.uri) for r in resources}
        assert 'petstore://pets' in uris


class TestResourceRead:
    """Invoking the resource read function reaches the upstream with substituted path params.

    Template reads (path params present) require an explicit ``ctx``.
    We pass a stub matching the existing tool-test pattern.
    Concrete reads (no path params) take zero arguments, driving the ``_NullContext`` fallback internally.
    """

    async def test_path_param_substituted_on_read(self, mock_upstream):
        """Template read with a stub ctx issues the GET with the path placeholder replaced."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={'id': 123, 'name': 'Rex'})

        mock_upstream(handler)

        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'})],
            x_mcp_integration=_expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        text = await read_fn(petId='123', ctx=_stub_context())
        assert captured['path'] == '/pets/123'
        assert '"id": 123' in text

    async def test_no_path_param_read_calls_upstream(self, mock_upstream):
        """Concrete read (zero arguments, NullContext internally) still reaches the upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={'available': 7})

        mock_upstream(handler)

        operation = OperationInfo(
            operation_id='getInventory',
            method='get',
            path='/store/inventory',
            x_mcp_integration=_expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        text = await read_fn()
        assert captured['path'] == '/store/inventory'
        assert '"available": 7' in text

    async def test_dashed_path_param_substituted_with_original_name(self, mock_upstream):
        """Sanitised identifier on the signature maps back to the original placeholder on the URL."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)

        operation = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[
                ParameterInfo(name='enterprise-team', location='path', required=True, schema={'type': 'string'}),
            ],
            x_mcp_integration=_expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        await read_fn(enterprise_team='acme', ctx=_stub_context())
        assert captured['path'] == '/teams/acme'

    async def test_upstream_error_raises(self, mock_upstream):
        """Non-2xx upstream responses surface as a ``RuntimeError`` from the read function."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={'error': 'not found'})

        mock_upstream(handler)

        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'})],
            x_mcp_integration=_expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamBinding(base_url='https://api.example.com'))
        with pytest.raises(RuntimeError, match='404'):
            await read_fn(petId='nope', ctx=_stub_context())


class TestResourceEligibilityValidation:
    """``_validate_resource_eligibility`` is the strict, fail-fast gate."""

    def test_non_get_rejected(self):
        """``expose.resource`` on a POST raises ``ValueError`` at startup."""
        operation = OperationInfo(
            operation_id='createPet',
            method='post',
            path='/pets',
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match='method is POST'):
            _validate_resource_eligibility(operation, 'petstore')

    @pytest.mark.parametrize('method', ['put', 'patch', 'delete'])
    def test_other_non_get_methods_rejected(self, method):
        """Every non-GET method is rejected with the same shape of error."""
        operation = OperationInfo(
            operation_id='changePet',
            method=method,
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match=f'method is {method.upper()}'):
            _validate_resource_eligibility(operation, 'petstore')

    def test_required_query_param_rejected(self):
        """A required query parameter on a resource-exposed GET is a config error."""
        operation = OperationInfo(
            operation_id='searchPets',
            method='get',
            path='/pets',
            parameters=[ParameterInfo(name='q', location='query', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match='required non-path parameter'):
            _validate_resource_eligibility(operation, 'petstore')

    def test_required_header_param_rejected(self):
        """A required header parameter is treated the same as a required query parameter."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[
                ParameterInfo(name='petId', location='path', required=True),
                ParameterInfo(name='X-Region', location='header', required=True),
            ],
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match='X-Region'):
            _validate_resource_eligibility(operation, 'petstore')

    def test_optional_query_param_allowed(self):
        """Optional non-path parameters are allowed (silently dropped from the resource surface)."""
        operation = OperationInfo(
            operation_id='listPets',
            method='get',
            path='/pets',
            parameters=[ParameterInfo(name='limit', location='query', required=False)],
            x_mcp_integration=_expose_resource(),
        )
        _validate_resource_eligibility(operation, 'petstore')  # no raise

    def test_uri_template_wrong_scheme_rejected(self):
        """An override URI template that escapes the server's scheme is rejected."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(uri_template='weather://current/{petId}'),
        )
        with pytest.raises(ValueError, match='must start with "petstore://"'):
            _validate_resource_eligibility(operation, 'petstore')


class TestPartitioning:
    """``_partition_operations`` slots each op into resource / tool / both based on ``promote_resources``."""

    def test_no_opt_in_under_auto_promotes_eligible_get(self):
        """Under ``mode=auto``, an eligible GET with no opt-in is auto-promoted to a resource."""
        op = OperationInfo(operation_id='get_pet', method='get', path='/pets/{petId}')
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == [op]
        assert tools == []

    def test_resource_only_replaces_tool(self):
        """``expose.resource`` alone moves the op into the resource bucket only."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == [op]
        assert tools == []

    def test_tool_only_stays_a_tool(self):
        """``expose.tool`` alone is the existing override path; op stays a tool."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=McpIntegration(tool=ToolOverride(name='fetch_pet')),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_both_opt_ins_registers_in_both(self):
        """Declaring both ``expose.tool`` and ``expose.resource`` places the op in both lists."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_tool_and_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == [op]
        assert tools == [op]

    def test_non_get_resource_raises(self):
        """Eligibility validation runs during partition; non-GET resources fail fast."""
        op = OperationInfo(
            operation_id='create_pet',
            method='post',
            path='/pets',
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match='method is POST'):
            _partition_operations([op], 'petstore', promote_resources=True)

    def test_required_query_resource_raises(self):
        """A required query param on a resource-exposed GET fails fast at partition time."""
        op = OperationInfo(
            operation_id='search_pets',
            method='get',
            path='/pets',
            parameters=[ParameterInfo(name='q', location='query', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        with pytest.raises(ValueError, match='required non-path parameter'):
            _partition_operations([op], 'petstore', promote_resources=True)

    def test_tool_only_ignores_resource_optin(self):
        """Under ``mode=tool_only`` (the default), ``expose.resource`` declarations are ignored."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=_expose_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=False)
        assert resources == []
        assert tools == [op]

    def test_default_mode_is_tool_only(self):
        """Calling without ``mode`` keyword defaults to ``tool_only``."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=_expose_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore')
        assert resources == []
        assert tools == [op]

    def test_auto_skips_ineligible_get(self):
        """Under ``mode=auto``, a GET with a required non-path parameter stays a tool."""
        op = OperationInfo(
            operation_id='find_pets',
            method='get',
            path='/pets',
            parameters=[ParameterInfo(name='status', location='query', required=True)],
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_auto_skips_non_get(self):
        """Under ``mode=auto``, a non-GET operation stays a tool."""
        op = OperationInfo(operation_id='create_pet', method='post', path='/pets')
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_auto_respects_explicit_tool_optin(self):
        """Under ``mode=auto``, an eligible GET with explicit ``expose.tool`` stays a tool (explicit beats implicit)."""
        op = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=McpIntegration(tool=ToolOverride()),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == []
        assert tools == [op]


class TestDualExposureRegistration:
    """When both ``expose.tool`` and ``expose.resource`` are present, both end up on the MCPServer."""

    async def test_op_appears_as_tool_and_resource(self):
        """``ToolGenerator`` and ``ResourceGenerator`` cooperate on the same MCPServer instance."""
        mcp = MCPServer('test')
        binding = UpstreamBinding(base_url='https://api.example.com')
        op = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'})],
            x_mcp_integration=_expose_tool_and_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        ResourceGenerator(mcp=mcp, binding=binding, server_name='petstore').register(resources)
        ToolGenerator(mcp=mcp, binding=binding).register(tools)

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'get_pet' in tool_names

        templates = await mcp.list_resource_templates()
        assert 'petstore://pets/{petId}' in {t.uri_template for t in templates}
