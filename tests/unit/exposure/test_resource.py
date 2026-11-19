import inspect

import httpx
import pytest
from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import (
    ResourceGenerator,
    UpstreamConnection,
    build_resource_read_function,
    derive_resource_mime_type,
    derive_resource_uri,
)
from openapi_mcp_gateway.openapi import (
    OperationInfo,
    ParameterInfo,
)
from tests.constants import API_URL
from tests.context import stub_context
from tests.operation import expose_resource


class TestResourceUriDerivation:
    """``derive_resource_uri`` produces MCPServer-compatible URI templates."""

    def test_no_path_params(self):
        """A path with no params yields a concrete URI under the server scheme."""
        operation = OperationInfo(
            operation_id='list_pets',
            method='get',
            path='/pets',
            x_mcp_integration=expose_resource(),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://pets'

    def test_single_path_param_preserved(self):
        """An identifier-safe path placeholder passes through unchanged."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=expose_resource(),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://pets/{petId}'

    def test_dashed_path_param_sanitized(self):
        """A dashed placeholder is rewritten to a Python identifier so MCPServer's regex matches."""
        operation = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[ParameterInfo(name='enterprise-team', location='path', required=True)],
            x_mcp_integration=expose_resource(),
        )
        assert derive_resource_uri('github', operation) == 'github://teams/{enterprise_team}'

    def test_uri_template_override_used_verbatim(self):
        """An explicit ``uri_template`` overrides the auto-derived URI exactly."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=expose_resource(uri_template='petstore://v2/pets/{petId}'),
        )
        assert derive_resource_uri('petstore', operation) == 'petstore://v2/pets/{petId}'


class TestResourceMimeType:
    """``derive_resource_mime_type`` defaults to JSON and honors overrides."""

    def test_default_is_json(self):
        """Default mime type is ``application/json``."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=expose_resource(),
        )
        assert derive_resource_mime_type(operation) == 'application/json'

    def test_override_wins(self):
        """An explicit ``mime_type`` override replaces the default."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            x_mcp_integration=expose_resource(mime_type='text/plain'),
        )
        assert derive_resource_mime_type(operation) == 'text/plain'


class TestResourceReadFunctionSignature:
    """Signature shape drives MCPServer's concrete-vs-template registration.

    - A GET with path params has the signature ``(p1, ..., ctx: Context)``, so it registers as a template,
      and MCPServer injects a real ``Context`` at call time.
    - A GET without path params has an empty signature, so it registers as concrete,
      and ``_NullContext`` is used internally.
    """

    def test_path_params_signature_includes_ctx(self):
        """Path-param signature is ``[path_params..., ctx]``, and query / body parameters are dropped."""
        operation = OperationInfo(
            operation_id='get_pet',
            method='get',
            path='/pets/{petId}',
            parameters=[
                ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'}),
                ParameterInfo(name='verbose', location='query', required=False, schema={'type': 'boolean'}),
            ],
            x_mcp_integration=expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        names = list(inspect.signature(fn).parameters)
        assert names == ['petId', 'ctx']

    def test_no_path_params_yields_empty_signature(self):
        """A GET with no path params has zero signature parameters, qualifying it as a concrete resource."""
        operation = OperationInfo(
            operation_id='get_inventory',
            method='get',
            path='/store/inventory',
            x_mcp_integration=expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
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
            x_mcp_integration=expose_resource(),
        )
        fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        assert 'enterprise_team' in inspect.signature(fn).parameters


class TestResourceGeneration:
    """End-to-end resource registration on an MCPServer instance."""

    def _generator(self) -> tuple[ResourceGenerator, MCPServer]:
        mcp = MCPServer('test')
        return (
            ResourceGenerator(
                mcp=mcp,
                connection=UpstreamConnection(base_url=API_URL),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        text = await read_fn(petId='123', ctx=stub_context())
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
            x_mcp_integration=expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        text = await read_fn()
        assert captured['path'] == '/store/inventory'
        assert '"available": 7' in text

    async def test_binary_read_returns_bytes(self, mock_upstream):
        """A binary body comes back as the upstream's bytes, which MCPServer serves as a blob."""
        body = b'\x89PNG\r\n\x1a\n\xff\xfe'
        mock_upstream(lambda _request: httpx.Response(200, content=body, headers={'content-type': 'image/png'}))

        operation = OperationInfo(
            operation_id='getLogo',
            method='get',
            path='/logo',
            x_mcp_integration=expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        assert await read_fn() == body

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
            x_mcp_integration=expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        await read_fn(enterprise_team='acme', ctx=stub_context())
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
            x_mcp_integration=expose_resource(),
        )
        read_fn = build_resource_read_function(operation, UpstreamConnection(base_url=API_URL))
        with pytest.raises(RuntimeError, match='404'):
            await read_fn(petId='nope', ctx=stub_context())
