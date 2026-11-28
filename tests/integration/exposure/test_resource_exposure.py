import httpx
import pytest
from mcp import Client
from mcp.types import TextResourceContents

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import ResourceOverride
from openapi_mcp_gateway.settings import ExposureConfig, GatewayConfig, OperationOverride, ServerConfig
from tests.constants import SHOP_URL
from tests.gateway import connect_in_memory, server_mcp, tool_names
from tests.spec import write_spec


class TestResourceExposureEndToEnd:
    """Full assembly chain for a spec opted into ``expose.resource``."""

    @pytest.fixture
    def gateway(self, tmp_path):
        spec_path = write_spec(tmp_path, _spec_with_resource_optin())
        return Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(name='shop', spec=str(spec_path), exposure=ExposureConfig(promote_resources=True))
                ]
            )
        )

    async def test_no_path_param_resource_listed_as_concrete(self, gateway):
        """A GET with no path params appears under ``resources/list`` (concrete), not templates."""
        async with connect_in_memory(gateway) as client:
            resources = (await client.list_resources()).resources
        uris = {str(r.uri) for r in resources}
        assert 'shop://store/inventory' in uris

    async def test_resources_excluded_from_tools(self, gateway):
        """Under ``exposure.promote_resources``, eligible GETs leave ``tools/list`` and ineligible ones stay.

        An eligible GET leaves whether or not it opts in.
        """
        listed = set(await tool_names(gateway))
        assert 'get_product' not in listed
        assert 'get_inventory' not in listed
        assert 'list_products' not in listed
        assert 'find_products' in listed

    async def test_template_metadata_from_spec(self, gateway):
        """A GET with a path param is listed as a template, its description and mime type from the spec."""
        async with connect_in_memory(gateway) as client:
            templates = (await client.list_resource_templates()).resource_templates
        template = next(t for t in templates if t.uri_template == 'shop://products/{productId}')
        assert template.description == 'Returns a single product record.'
        assert template.mime_type == 'application/json'


class TestDualExposureEndToEnd:
    """Declaring both ``expose.tool`` and ``expose.resource`` registers the op in both surfaces."""

    @pytest.fixture
    def gateway(self, tmp_path):
        spec_path = write_spec(tmp_path, _spec_with_dual_optin())
        return Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(name='shop', spec=str(spec_path), exposure=ExposureConfig(promote_resources=True))
                ]
            )
        )

    async def test_tool_and_resource_both_present(self, gateway):
        """The operation surfaces as a tool (under its overridden name) AND as a resource template."""
        assert 'fetch_product' in await tool_names(gateway)

        async with connect_in_memory(gateway) as client:
            templates = (await client.list_resource_templates()).resource_templates
        assert 'shop://products/{productId}' in {t.uri_template for t in templates}


class TestResourceUpstreamCall:
    """The resource read function reaches the upstream with the path placeholder substituted.

    Goes through the full Gateway assembly,
    so the auth resolver, base URL, and URI-template-driven kwarg passing are all exercised together.
    """

    async def test_read_concrete_resource_calls_upstream(self, tmp_path, mock_upstream):
        """Reading a concrete URI issues ``GET /store/inventory`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'available': 7, 'sold': 3})

        mock_upstream(handler)

        spec_path = write_spec(tmp_path, _spec_with_resource_optin())
        gateway = Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(name='shop', spec=str(spec_path), exposure=ExposureConfig(promote_resources=True))
                ]
            )
        )
        async with Client(server_mcp(gateway)) as client:
            result = await client.read_resource('shop://store/inventory')
        assert captured['url'].startswith(f'{SHOP_URL}/store/inventory')
        [contents] = result.contents
        assert isinstance(contents, TextResourceContents)
        assert '"available": 7' in contents.text

    async def test_read_template_calls_upstream(self, tmp_path, mock_upstream):
        """A template-resource read issues ``GET /products/<id>`` upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'id': 42, 'name': 'Rex'})

        mock_upstream(handler)

        spec_path = write_spec(tmp_path, _spec_with_resource_optin())
        gateway = Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(name='shop', spec=str(spec_path), exposure=ExposureConfig(promote_resources=True))
                ]
            )
        )
        async with Client(server_mcp(gateway)) as client:
            result = await client.read_resource('shop://products/42')
        assert captured['method'] == 'GET'
        assert captured['url'].startswith(f'{SHOP_URL}/products/42')
        [contents] = result.contents
        assert isinstance(contents, TextResourceContents)
        assert '"id": 42' in contents.text


class TestYamlOverrideEndToEnd:
    """``ServerConfig.operations`` injects per-op ``x-mcp-integration`` overrides without touching the spec."""

    async def test_yaml_override_renames_auto_promoted_resource(self, tmp_path):
        """YAML ``operations.<id>.expose.resource.name`` renames the resource that promotion registered."""
        spec_path = write_spec(tmp_path, _vanilla_spec())
        gateway = Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(
                        name='shop',
                        spec=str(spec_path),
                        exposure=ExposureConfig(promote_resources=True),
                        operations={
                            'getProduct': OperationOverride(
                                resource=ResourceOverride(name='product', mime_type='application/json'),
                            ),
                        },
                    )
                ]
            )
        )
        async with connect_in_memory(gateway) as client:
            templates = (await client.list_resource_templates()).resource_templates
        names = {template.name for template in templates}
        assert 'product' in names


def _spec_with_resource_optin() -> dict:
    """OpenAPI spec exercising both kinds of resource exposure plus eligibility edge cases.

    - ``listProducts`` (GET /products, no opt-in, no required params): under ``exposure.promote_resources`` auto-promotes to a resource.
    - ``findProducts`` (GET /products/find, no opt-in, required query): not eligible, stays a tool even under ``exposure.promote_resources``.
    - ``getProduct`` (GET /products/{productId}, explicit ``expose.resource``): templated resource.
    - ``getInventory`` (GET /store/inventory, explicit ``expose.resource``): concrete resource.
    """
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Shop Resource', 'version': '1.0.0'},
        'servers': [{'url': SHOP_URL}],
        'paths': {
            '/products': {
                'get': {
                    'operationId': 'listProducts',
                    'summary': 'List products',
                    'responses': {'200': {'description': 'ok'}},
                },
            },
            '/products/find': {
                'get': {
                    'operationId': 'findProducts',
                    'summary': 'Search products by status',
                    'parameters': [
                        {
                            'name': 'status',
                            'in': 'query',
                            'required': True,
                            'schema': {'type': 'string'},
                        },
                    ],
                    'responses': {'200': {'description': 'ok'}},
                },
            },
            '/products/{productId}': {
                'get': {
                    'operationId': 'getProduct',
                    'summary': 'Fetch one product by id',
                    'description': 'Returns a single product record.',
                    'parameters': [
                        {
                            'name': 'productId',
                            'in': 'path',
                            'required': True,
                            'schema': {'type': 'string'},
                        },
                    ],
                    'x-mcp-integration': {
                        'resource': {
                            'name': 'product',
                            'mime_type': 'application/json',
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
                        'resource': {
                            'name': 'inventory',
                            'mime_type': 'application/json',
                        },
                    },
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }


def _spec_with_dual_optin() -> dict:
    """Spec where ``getProduct`` declares both ``expose.tool`` and ``expose.resource``."""
    spec = _spec_with_resource_optin()
    spec['paths']['/products/{productId}']['get']['x-mcp-integration']['tool'] = {
        'name': 'fetch_product',
    }
    return spec


def _vanilla_spec() -> dict:
    """Vanilla spec without any ``x-mcp-integration`` declarations, for YAML-override tests."""
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Shop', 'version': '1.0.0'},
        'servers': [{'url': SHOP_URL}],
        'paths': {
            '/products/{productId}': {
                'get': {
                    'operationId': 'getProduct',
                    'parameters': [
                        {'name': 'productId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }
