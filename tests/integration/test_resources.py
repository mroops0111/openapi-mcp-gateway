import json
import typing

import httpx
import pytest
from mcp.server.fastmcp import Context

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig, ServerConfig


def _spec_with_resource_optin() -> dict:
    """OpenAPI spec exercising both kinds of resource exposure plus a regular tool.

    - ``listPets`` (GET /pets): no opt-in, stays a tool.
    - ``getPet`` (GET /pets/{petId}): templated resource.
    - ``getInventory`` (GET /store/inventory): concrete resource (no path params).
    """
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Petstore Resource', 'version': '1.0.0'},
        'servers': [{'url': 'https://petstore.example.com/v1'}],
        'paths': {
            '/pets': {
                'get': {
                    'operationId': 'listPets',
                    'summary': 'List pets',
                    'responses': {'200': {'description': 'ok'}},
                },
            },
            '/pets/{petId}': {
                'get': {
                    'operationId': 'getPet',
                    'summary': 'Fetch one pet by id',
                    'description': 'Returns a single pet record.',
                    'parameters': [
                        {
                            'name': 'petId',
                            'in': 'path',
                            'required': True,
                            'schema': {'type': 'string'},
                        },
                    ],
                    'x-mcp-integration': {
                        'expose': {
                            'resource': {
                                'name': 'pet',
                                'mime_type': 'application/json',
                            },
                        },
                    },
                    'responses': {'200': {'description': 'ok'}},
                },
            },
            '/store/inventory': {
                'get': {
                    'operationId': 'getInventory',
                    'summary': 'Aggregate inventory snapshot',
                    'x-mcp-integration': {
                        'expose': {
                            'resource': {
                                'name': 'inventory',
                                'mime_type': 'application/json',
                            },
                        },
                    },
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }


def _spec_with_dual_optin() -> dict:
    """Spec where ``getPet`` declares both ``expose.tool`` and ``expose.resource``."""
    spec = _spec_with_resource_optin()
    spec['paths']['/pets/{petId}']['get']['x-mcp-integration']['expose']['tool'] = {
        'name': 'fetch_pet',
    }
    return spec


def _spec_with_misconfig_post_resource() -> dict:
    """Spec where ``createPet`` (POST) wrongly declares ``expose.resource``."""
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Bad Resource', 'version': '1.0.0'},
        'servers': [{'url': 'https://petstore.example.com/v1'}],
        'paths': {
            '/pets': {
                'post': {
                    'operationId': 'createPet',
                    'x-mcp-integration': {'expose': {'resource': {}}},
                    'responses': {'201': {'description': 'created'}},
                },
            },
        },
    }


def _write_spec(tmp_path, spec: dict):
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(spec))
    return path


class TestResourceExposureEndToEnd:
    """Full assembly chain for a spec opted into ``expose.resource``."""

    @pytest.fixture
    def gateway(self, tmp_path):
        spec_path = _write_spec(tmp_path, _spec_with_resource_optin())
        return Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path))]))

    async def test_path_param_resource_listed_as_template(self, gateway):
        """A GET with a path param appears under ``resources/templates/list`` with the derived URI."""
        mcp = gateway._servers[0].mcp
        templates = await mcp.list_resource_templates()
        uris = {t.uriTemplate for t in templates}
        assert 'petstore://pets/{petId}' in uris

    async def test_no_path_param_resource_listed_as_concrete(self, gateway):
        """A GET with no path params appears under ``resources/list`` (concrete), not templates."""
        mcp = gateway._servers[0].mcp
        resources = await mcp.list_resources()
        uris = {str(r.uri) for r in resources}
        assert 'petstore://store/inventory' in uris

    async def test_resources_excluded_from_tools(self, gateway):
        """Opted-in GETs disappear from ``tools/list``; non-opted-in GET remains."""
        mcp = gateway._servers[0].mcp
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'get_pet' not in tool_names
        assert 'get_inventory' not in tool_names
        assert 'list_pets' in tool_names

    async def test_template_metadata_from_spec(self, gateway):
        """Description and mime type fall back to spec values when not overridden in the resource block."""
        mcp = gateway._servers[0].mcp
        templates = await mcp.list_resource_templates()
        template = next(t for t in templates if t.uriTemplate == 'petstore://pets/{petId}')
        assert template.description == 'Returns a single pet record.'
        assert template.mimeType == 'application/json'


class TestDualExposureEndToEnd:
    """Declaring both ``expose.tool`` and ``expose.resource`` registers the op in both surfaces."""

    @pytest.fixture
    def gateway(self, tmp_path):
        spec_path = _write_spec(tmp_path, _spec_with_dual_optin())
        return Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path))]))

    async def test_tool_and_resource_both_present(self, gateway):
        """The operation surfaces as a tool (under its overridden name) AND as a resource template."""
        mcp = gateway._servers[0].mcp
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'fetch_pet' in tool_names

        templates = await mcp.list_resource_templates()
        assert 'petstore://pets/{petId}' in {t.uriTemplate for t in templates}


class TestResourceMisconfigFailsFast:
    """Phase-0-style strict validation: misconfigured resources refuse to start."""

    def test_post_with_expose_resource_raises(self, tmp_path):
        """A POST opted into ``expose.resource`` aborts ``Gateway.from_config`` with a clear error."""
        spec_path = _write_spec(tmp_path, _spec_with_misconfig_post_resource())
        with pytest.raises(ValueError, match='method is POST'):
            Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path))]))


class TestResourceUpstreamCall:
    """The resource read function reaches the upstream with the path placeholder substituted.

    Goes through the full Gateway assembly,
    so the auth resolver, base URL, and URI-template-driven kwarg passing are all exercised together.
    """

    async def test_read_concrete_resource_calls_upstream(self, tmp_path, mock_upstream):
        """``mcp.read_resource`` against a concrete URI issues ``GET /store/inventory`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'available': 7, 'sold': 3})

        mock_upstream(handler)

        spec_path = _write_spec(tmp_path, _spec_with_resource_optin())
        gateway = Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path))]))
        mcp = gateway._servers[0].mcp
        contents = list(await mcp.read_resource('petstore://store/inventory'))
        assert captured['url'].startswith('https://petstore.example.com/v1/store/inventory')
        assert len(contents) == 1
        body = contents[0].content
        assert isinstance(body, str)
        assert '"available": 7' in body

    async def test_read_template_calls_upstream(self, tmp_path, mock_upstream):
        """A template-resource read with a stub ctx issues ``GET /pets/<id>`` upstream.

        ``mcp.read_resource`` would FastMCP-inject a real ``Context``,
        whose ``request_context`` is unset outside a live JSON-RPC session,
        so the closure's ``ctx.report_progress`` call would raise.
        We instead pull the registered template's underlying function and invoke it with a stub ctx,
        matching the tool integration tests in ``test_gateway.py``.
        """
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'id': 42, 'name': 'Rex'})

        mock_upstream(handler)

        spec_path = _write_spec(tmp_path, _spec_with_resource_optin())
        gateway = Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path))]))
        mcp = gateway._servers[0].mcp
        template = mcp._resource_manager._templates['petstore://pets/{petId}']
        raw_fn = template.fn.raw_function

        class _Ctx:
            async def report_progress(self, *_args, **_kwargs):
                return None

        text = await raw_fn(petId='42', ctx=typing.cast(Context, _Ctx()))
        assert captured['method'] == 'GET'
        assert captured['url'].startswith('https://petstore.example.com/v1/pets/42')
        assert '"id": 42' in text
