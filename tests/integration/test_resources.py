import json
import typing

import httpx
import pytest
from mcp.server.mcpserver import Context
from mcp.types import InputRequiredResult

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import McpIntegration, ResourceOverride
from openapi_mcp_gateway.settings import GatewayConfig, ServerConfig


class _StubContext:
    """No-op MCP context used when invoking generated resource read functions end-to-end."""

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context() -> Context:
    """Return a ``Context``-typed stub suitable for invoking resource read functions directly."""
    return typing.cast(Context, _StubContext())


def _spec_with_resource_optin() -> dict:
    """OpenAPI spec exercising both kinds of resource exposure plus eligibility edge cases.

    - ``listPets`` (GET /pets, no opt-in, no required params): under ``mode=auto`` auto-promotes to a resource.
    - ``findPets`` (GET /pets/find, no opt-in, required query): not eligible, stays a tool even under ``mode=auto``.
    - ``getPet`` (GET /pets/{petId}, explicit ``expose.resource``): templated resource.
    - ``getInventory`` (GET /store/inventory, explicit ``expose.resource``): concrete resource.
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
            '/pets/find': {
                'get': {
                    'operationId': 'findPets',
                    'summary': 'Search pets by status',
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
                        'resource': {
                            'name': 'pet',
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
    """Spec where ``getPet`` declares both ``expose.tool`` and ``expose.resource``."""
    spec = _spec_with_resource_optin()
    spec['paths']['/pets/{petId}']['get']['x-mcp-integration']['tool'] = {
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
                    'x-mcp-integration': {'resource': {}},
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
        return Gateway.from_config(
            GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path), mode='auto')])
        )

    async def test_path_param_resource_listed_as_template(self, gateway):
        """A GET with a path param appears under ``resources/templates/list`` with the derived URI."""
        mcp = gateway._servers[0].mcp
        templates = await mcp.list_resource_templates()
        uris = {t.uri_template for t in templates}
        assert 'petstore://pets/{petId}' in uris

    async def test_no_path_param_resource_listed_as_concrete(self, gateway):
        """A GET with no path params appears under ``resources/list`` (concrete), not templates."""
        mcp = gateway._servers[0].mcp
        resources = await mcp.list_resources()
        uris = {str(r.uri) for r in resources}
        assert 'petstore://store/inventory' in uris

    async def test_resources_excluded_from_tools(self, gateway):
        """Under ``mode=auto``, every eligible GET (opt-in or not) leaves ``tools/list``; ineligible GETs stay."""
        mcp = gateway._servers[0].mcp
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'get_pet' not in tool_names
        assert 'get_inventory' not in tool_names
        assert 'list_pets' not in tool_names
        assert 'find_pets' in tool_names

    async def test_template_metadata_from_spec(self, gateway):
        """Description and mime type fall back to spec values when not overridden in the resource block."""
        mcp = gateway._servers[0].mcp
        templates = await mcp.list_resource_templates()
        template = next(t for t in templates if t.uri_template == 'petstore://pets/{petId}')
        assert template.description == 'Returns a single pet record.'
        assert template.mime_type == 'application/json'


class TestDualExposureEndToEnd:
    """Declaring both ``expose.tool`` and ``expose.resource`` registers the op in both surfaces."""

    @pytest.fixture
    def gateway(self, tmp_path):
        spec_path = _write_spec(tmp_path, _spec_with_dual_optin())
        return Gateway.from_config(
            GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path), mode='auto')])
        )

    async def test_tool_and_resource_both_present(self, gateway):
        """The operation surfaces as a tool (under its overridden name) AND as a resource template."""
        mcp = gateway._servers[0].mcp
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'fetch_pet' in tool_names

        templates = await mcp.list_resource_templates()
        assert 'petstore://pets/{petId}' in {t.uri_template for t in templates}


class TestResourceMisconfigFailsFast:
    """Phase-0-style strict validation: misconfigured resources refuse to start."""

    def test_post_with_expose_resource_raises(self, tmp_path):
        """A POST opted into ``expose.resource`` aborts ``Gateway.from_config`` with a clear error."""
        spec_path = _write_spec(tmp_path, _spec_with_misconfig_post_resource())
        with pytest.raises(ValueError, match='method is POST'):
            Gateway.from_config(
                GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path), mode='auto')])
            )


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
        gateway = Gateway.from_config(
            GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path), mode='auto')])
        )
        mcp = gateway._servers[0].mcp
        result = await mcp.read_resource('petstore://store/inventory')
        # mcp v2 widened read_resource to also return an InputRequiredResult, the MRTR interim result.
        # This read never triggers one, so narrow to the resource-contents arm.
        assert not isinstance(result, InputRequiredResult)
        contents = list(result)
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
        gateway = Gateway.from_config(
            GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(spec_path), mode='auto')])
        )
        mcp = gateway._servers[0].mcp
        template = mcp._resource_manager._templates['petstore://pets/{petId}']
        raw_fn = template.fn.raw_function

        text = await raw_fn(petId='42', ctx=_stub_context())
        assert captured['method'] == 'GET'
        assert captured['url'].startswith('https://petstore.example.com/v1/pets/42')
        assert '"id": 42' in text


def _vanilla_spec() -> dict:
    """Vanilla spec without any ``x-mcp-integration`` declarations, for YAML-override tests."""
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Petstore', 'version': '1.0.0'},
        'servers': [{'url': 'https://petstore.example.com/v1'}],
        'paths': {
            '/pets/{petId}': {
                'get': {
                    'operationId': 'getPet',
                    'parameters': [
                        {'name': 'petId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }


class TestYamlOverrideEndToEnd:
    """``ServerConfig.operations`` injects per-op ``x-mcp-integration`` overrides without touching the spec."""

    async def test_yaml_override_renames_auto_promoted_resource(self, tmp_path):
        """YAML ``operations.<id>.expose.resource.name`` renames the resource registered by ``mode='auto'``."""
        spec_path = _write_spec(tmp_path, _vanilla_spec())
        gateway = Gateway.from_config(
            GatewayConfig(
                servers=[
                    ServerConfig(
                        name='petstore',
                        spec=str(spec_path),
                        mode='auto',
                        operations={
                            'getPet': McpIntegration(
                                resource=ResourceOverride(name='pet', mime_type='application/json'),
                            ),
                        },
                    )
                ]
            )
        )
        templates = await gateway._servers[0].mcp.list_resource_templates()
        names = {template.name for template in templates}
        assert 'pet' in names

    def test_unknown_operation_id_in_yaml_aborts(self, tmp_path):
        """A YAML override targeting an op that the spec does not expose fails ``Gateway.from_config``."""
        spec_path = _write_spec(tmp_path, _vanilla_spec())
        with pytest.raises(ValueError, match='not_a_real_op'):
            Gateway.from_config(
                GatewayConfig(
                    servers=[
                        ServerConfig(
                            name='petstore',
                            spec=str(spec_path),
                            mode='auto',
                            operations={
                                'not_a_real_op': McpIntegration(
                                    resource=ResourceOverride(name='ghost'),
                                ),
                            },
                        )
                    ]
                )
            )
