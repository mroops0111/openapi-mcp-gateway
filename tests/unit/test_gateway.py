import json

import pytest
from fastapi import FastAPI

from openapi_mcp_gateway import Gateway
from openapi_mcp_gateway.exposure import build_input_schema
from openapi_mcp_gateway.gateway import (
    apply_yaml_overrides,
    partition_operations,
    validate_resource_eligibility,
)
from openapi_mcp_gateway.openapi import (
    McpIntegration,
    OperationInfo,
    ParameterInfo,
    ResourceOverride,
    ToolOverride,
)
from openapi_mcp_gateway.settings import GatewayConfig, OperationOverride, ServerConfig
from tests.operation import expose_resource
from tests.spec import defs_ref, nested_body_spec, shared_component_spec, write_spec


# The securitySchemes of a spec that takes one OAuth token.
OAUTH2_SCHEMES = {'oauth2': {'type': 'oauth2'}}


class TestResourceEligibilityValidation:
    """``validate_resource_eligibility`` is the strict, fail-fast gate."""

    @pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
    def test_non_get_methods_rejected(self, method):
        """``expose.resource`` on any method but GET raises ``ValueError`` at startup."""
        operation = OperationInfo(
            operation_id='changeProduct',
            method=method,
            path='/products/{productId}',
            parameters=[ParameterInfo(name='productId', location='path', required=True)],
            x_mcp_integration=expose_resource(),
        )
        with pytest.raises(ValueError, match=f'method is {method.upper()}'):
            validate_resource_eligibility(operation, 'shop')

    def test_required_query_param_rejected(self):
        """A required query parameter on a resource-exposed GET is a config error."""
        operation = OperationInfo(
            operation_id='searchProducts',
            method='get',
            path='/products',
            parameters=[ParameterInfo(name='q', location='query', required=True)],
            x_mcp_integration=expose_resource(),
        )
        with pytest.raises(ValueError, match='required non-path parameter'):
            validate_resource_eligibility(operation, 'shop')

    def test_required_header_param_rejected(self):
        """A required header parameter is treated the same as a required query parameter."""
        operation = OperationInfo(
            operation_id='getProduct',
            method='get',
            path='/products/{productId}',
            parameters=[
                ParameterInfo(name='productId', location='path', required=True),
                ParameterInfo(name='X-Region', location='header', required=True),
            ],
            x_mcp_integration=expose_resource(),
        )
        with pytest.raises(ValueError, match='X-Region'):
            validate_resource_eligibility(operation, 'shop')

    def test_optional_query_param_allowed(self):
        """Optional non-path parameters are allowed (silently dropped from the resource surface)."""
        operation = OperationInfo(
            operation_id='listProducts',
            method='get',
            path='/products',
            parameters=[ParameterInfo(name='limit', location='query', required=False)],
            x_mcp_integration=expose_resource(),
        )
        validate_resource_eligibility(operation, 'shop')  # no raise

    def test_uri_template_wrong_scheme_rejected(self):
        """An override URI template that escapes the server's scheme is rejected."""
        operation = OperationInfo(
            operation_id='getProduct',
            method='get',
            path='/products/{productId}',
            parameters=[ParameterInfo(name='productId', location='path', required=True)],
            x_mcp_integration=expose_resource(uri_template='weather://current/{productId}'),
        )
        with pytest.raises(ValueError, match='must start with "shop://"'):
            validate_resource_eligibility(operation, 'shop')


class TestPartitioning:
    """``partition_operations`` slots each op into resource / tool / both based on ``promote_resources``."""

    def test_no_opt_in_under_auto_promotes_eligible_get(self):
        """Under ``exposure.promote_resources``, an eligible GET with no opt-in is auto-promoted to a resource."""
        op = OperationInfo(operation_id='get_product', method='get', path='/products/{productId}')
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == [op]
        assert tools == []

    def test_resource_only_replaces_tool(self):
        """``expose.resource`` alone moves the op into the resource bucket only."""
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            parameters=[ParameterInfo(name='productId', location='path', required=True)],
            x_mcp_integration=expose_resource(),
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == [op]
        assert tools == []

    def test_tool_only_stays_a_tool(self):
        """``expose.tool`` alone is the existing override path, so op stays a tool."""
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            x_mcp_integration=McpIntegration(tool=ToolOverride(name='fetch_product')),
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_both_opt_ins_registers_in_both(self):
        """Declaring both ``expose.tool`` and ``expose.resource`` places the op in both lists."""
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            parameters=[ParameterInfo(name='productId', location='path', required=True)],
            x_mcp_integration=_expose_tool_and_resource(),
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == [op]
        assert tools == [op]

    def test_non_get_resource_raises(self):
        """Eligibility validation runs during partition, so non-GET resources fail fast."""
        op = OperationInfo(
            operation_id='create_product',
            method='post',
            path='/products',
            x_mcp_integration=expose_resource(),
        )
        with pytest.raises(ValueError, match='method is POST'):
            partition_operations([op], 'shop', promote_resources=True)

    def test_without_promotion_resource_opt_ins_stay_tools(self):
        """Without ``exposure.promote_resources``, ``expose.resource`` declarations are ignored."""
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            parameters=[ParameterInfo(name='productId', location='path', required=True)],
            x_mcp_integration=expose_resource(),
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=False)
        assert resources == []
        assert tools == [op]

    def test_promotion_is_off_by_default(self):
        """Calling without ``promote_resources`` leaves every operation a tool."""
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            x_mcp_integration=expose_resource(),
        )
        resources, tools = partition_operations([op], 'shop')
        assert resources == []
        assert tools == [op]

    def test_auto_skips_ineligible_get(self):
        """Under ``exposure.promote_resources``, a GET with a required non-path parameter stays a tool."""
        op = OperationInfo(
            operation_id='find_products',
            method='get',
            path='/products',
            parameters=[ParameterInfo(name='status', location='query', required=True)],
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_auto_skips_non_get(self):
        """Under ``exposure.promote_resources``, a non-GET operation stays a tool."""
        op = OperationInfo(operation_id='create_product', method='post', path='/products')
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_auto_respects_explicit_tool_optin(self):
        """Under ``exposure.promote_resources``, an eligible GET with explicit ``expose.tool`` stays a tool.

        Explicit opt-in beats the implicit promotion.
        """
        op = OperationInfo(
            operation_id='get_product',
            method='get',
            path='/products/{productId}',
            x_mcp_integration=McpIntegration(tool=ToolOverride()),
        )
        resources, tools = partition_operations([op], 'shop', promote_resources=True)
        assert resources == []
        assert tools == [op]


class TestApplyYamlOverrides:
    """``apply_yaml_overrides`` swaps in the YAML-side ``x-mcp-integration`` and ``security`` for each named operation."""

    def test_empty_overrides_returns_same_list(self):
        """An empty YAML override map leaves operations untouched."""
        operations = [_operation('get_product')]
        result = apply_yaml_overrides(operations, {}, 'shop', {})
        assert result is operations

    def test_single_override_applied(self):
        """A YAML override for one op replaces its ``x_mcp_integration``."""
        operations = [_operation('get_product'), _operation('list_products')]
        override = OperationOverride(resource=ResourceOverride(name='product'))
        result = apply_yaml_overrides(operations, {'get_product': override}, 'shop', {})
        assert result[0].x_mcp_integration == McpIntegration(resource=override.resource)
        assert result[1].x_mcp_integration is operations[1].x_mcp_integration

    def test_replace_does_not_merge_with_spec(self):
        """YAML override fully replaces spec-side ``x_mcp_integration`` (no merge)."""
        spec_side = McpIntegration(tool=ToolOverride(name='spec_name'))
        operations = [_operation('get_product', spec_side)]
        yaml_side = OperationOverride(resource=ResourceOverride(name='yaml_name'))
        result = apply_yaml_overrides(operations, {'get_product': yaml_side}, 'shop', {})
        # YAML wins, so the resource is present and the original spec-side tool override is gone.
        integration = result[0].x_mcp_integration
        assert integration.tool is None
        assert integration.resource is not None
        assert integration.resource.name == 'yaml_name'

    def test_multiple_overrides_applied(self):
        """Several overrides apply to their respective ops, leaving unmentioned ops untouched."""
        operations = [_operation('a'), _operation('b'), _operation('c')]
        overrides = {
            'a': OperationOverride(resource=ResourceOverride(name='alpha')),
            'c': OperationOverride(tool=ToolOverride(name='gamma')),
        }
        result = apply_yaml_overrides(operations, overrides, 'srv', {})
        first = result[0].x_mcp_integration
        assert first.resource is not None
        assert first.resource.name == 'alpha'
        assert result[1].x_mcp_integration.tool is None
        assert result[1].x_mcp_integration.resource is None
        third = result[2].x_mcp_integration
        assert third.tool is not None
        assert third.tool.name == 'gamma'

    def test_unmatched_operation_id_raises(self):
        """Override that names an op not present in the server's spec aborts with a clear ``ValueError``."""
        operations = [_operation('get_product')]
        overrides = {'unknown_op': OperationOverride()}
        with pytest.raises(ValueError, match='unknown_op'):
            apply_yaml_overrides(operations, overrides, 'shop', {})

    def test_unmatched_error_lists_all_offending_ids(self):
        """Multiple unmatched op_ids appear in the error message together."""
        operations = [_operation('get_product')]
        overrides = {'wrong_a': OperationOverride(), 'wrong_b': OperationOverride()}
        with pytest.raises(ValueError, match=r'wrong_a.*wrong_b'):
            apply_yaml_overrides(operations, overrides, 'shop', {})

    def test_does_not_mutate_input_operations(self):
        """The helper returns a new list and does not mutate the input ``OperationInfo`` instances."""
        operations = [_operation('get_product')]
        original_integration = operations[0].x_mcp_integration
        override = OperationOverride(resource=ResourceOverride(name='product'))
        apply_yaml_overrides(operations, {'get_product': override}, 'shop', {})
        assert operations[0].x_mcp_integration is original_integration

    def test_a_security_override_alone_keeps_the_spec_integration(self):
        """Overriding ``security`` replaces the operation's requirements and leaves ``x-mcp-integration`` as it is."""
        spec_side = McpIntegration(tool=ToolOverride(name='spec_name'))
        operations = [_operation('get_product', spec_side)]
        security = [{'oauth2': ['products:read']}, {'oauth2': ['products:admin']}]
        [result] = apply_yaml_overrides(
            operations, {'get_product': OperationOverride(security=security)}, 'shop', OAUTH2_SCHEMES
        )
        assert result.security == security
        assert result.x_mcp_integration is spec_side

    def test_a_security_override_naming_an_undeclared_scheme_raises(self):
        """A misspelt scheme would otherwise count as a credential the gateway lacks, hiding the operation."""
        operations = [_operation('get_product')]
        override = OperationOverride(security=[{'OAuth2': ['products:read']}])
        with pytest.raises(ValueError, match=r"operations\.get_product\.security .* \['OAuth2'\]"):
            apply_yaml_overrides(operations, {'get_product': override}, 'shop', OAUTH2_SCHEMES)


class TestGatewayAssembly:
    """Server registration, spec parsing, auth provider wiring, and policy validation."""

    def test_servers_registered(self, shop_json_path):
        """A configured server is described with its derived mount path."""
        gateway = _shop_gateway(shop_json_path)
        assert len(gateway.describe_servers()) == 1
        assert gateway.describe_servers()[0].name == 'shop'
        assert gateway.describe_servers()[0].mount_path == '/shop'

    def test_no_auth_provider_for_no_auth(self, shop_json_path):
        """A server without auth config has no authorization server attached."""
        gateway = _shop_gateway(shop_json_path)
        assert gateway.describe_servers()[0].auth.provider is None

    def test_multiple_servers(self, shop_json_path):
        """Multiple servers register on independent mount paths."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='products', spec=str(shop_json_path)),
                ServerConfig(name='products2', spec=str(shop_json_path), path_prefix='other'),
            ],
        )
        gateway = Gateway.from_config(config)
        assert len(gateway.describe_servers()) == 2
        paths = [server.mount_path for server in gateway.describe_servers()]
        assert '/products' in paths
        assert '/other' in paths


class TestExposedToolDetail:
    """A caller deciding what to expose reads the same contract the model is given."""

    def test_the_description_is_the_one_the_model_receives(self, shop_json_path):
        """Reporting anything else would let the preview and the served tool disagree."""
        gateway = Gateway()
        gateway.add_server(name='products', spec=str(shop_json_path))
        tool = next(t for t in gateway.describe_servers()[0].tools if t.name == 'get_product_by_id')

        assert tool.description
        assert tool.description != tool.name

    def test_the_schema_is_the_advertised_one_rather_than_a_copy(self, shop_json_path):
        """A second representation is a second thing to keep in step, so there is only one."""
        gateway = Gateway()
        gateway.add_server(name='products', spec=str(shop_json_path))
        bundle = gateway.describe_servers()[0]
        tool = next(t for t in bundle.tools if t.name == 'get_product_by_id')
        operation = next(o for o in bundle.spec.operations if o.operation_id == 'getProductById')

        assert tool.input_schema == build_input_schema(operation)

    def test_a_nested_body_keeps_its_shape(self, tmp_path):
        """Flattening body properties into a list loses exactly what a reviewer needs.

        A summary that reports ``priority: string`` cannot answer what the model may send.
        The answer lives below the top level, in an enum, a default, a pattern and a numeric bound.
        """
        spec = write_spec(tmp_path, nested_body_spec())
        gateway = Gateway()
        gateway.add_server(name='o', spec=str(spec))
        schema = gateway.describe_servers()[0].tools[0].input_schema

        assert schema is not None
        assert schema['properties']['priority']['enum'] == ['low', 'normal', 'rush']
        assert schema['properties']['priority']['default'] == 'normal'
        assert schema['properties']['customer']['properties']['id']['type'] == 'integer'
        assert schema['properties']['items']['items']['properties']['qty']['minimum'] == 1
        assert schema['required'] == ['customer']


class TestDescribedSchema:
    """``describe()`` reports the schema in the form a client receives."""

    def test_a_tool_without_repetition_has_no_defs(self, shop_json_path):
        """A spec with no shape reached twice advertises the fully inlined schema, unchanged from before."""
        gateway = Gateway()
        gateway.add_server(name='products', spec=str(shop_json_path))

        for tool in gateway.describe_servers()[0].tools:
            assert tool.input_schema is not None
            assert '$defs' not in tool.input_schema
            assert '$ref' not in json.dumps(tool.input_schema)

    def test_the_dry_run_summary_shows_the_advertised_form(self, tmp_path):
        """``describe()`` reports the same ``$defs`` form a client receives, not an expanded copy."""
        spec = write_spec(tmp_path, shared_component_spec())
        gateway = Gateway()
        gateway.add_server(name='p', spec=str(spec))

        schema = gateway.describe()['servers'][0]['tools'][0]['input_schema']

        assert schema['properties']['payload'] == defs_ref('SourceReference')
        assert set(schema['$defs']) == {'SourceReference', 'Node'}


class TestMountTransport:
    """Mounting into a host app warns about the deprecated transport and only about it."""

    def test_sse_transport_emits_deprecation_warning(self, shop_json_path):
        """Selecting the deprecated ``sse`` transport warns the caller."""
        gateway = _shop_gateway(shop_json_path)
        with pytest.warns(DeprecationWarning, match='sse'):
            gateway.mount(FastAPI(), transport='sse')

    def test_streamable_http_transport_does_not_warn(self, shop_json_path, recwarn):
        """The recommended ``streamable-http`` transport mounts without an SSE deprecation warning."""
        gateway = _shop_gateway(shop_json_path)
        gateway.mount(FastAPI(), transport='streamable-http')
        sse_warnings = [
            warning
            for warning in recwarn
            if issubclass(warning.category, DeprecationWarning) and 'sse' in str(warning.message)
        ]
        assert not sse_warnings


class TestBuildApp:
    """``build_app`` serves an HTTP transport, warning about the deprecated one as ``run`` does."""

    def test_sse_transport_emits_deprecation_warning(self, shop_json_path):
        with pytest.warns(DeprecationWarning, match='sse'):
            _shop_gateway(shop_json_path).build_app(transport='sse')

    def test_stdio_is_refused(self, shop_json_path):
        with pytest.raises(ValueError, match='stdio transport serves no HTTP app'):
            _shop_gateway(shop_json_path).build_app(transport='stdio')


def _shop_gateway(shop_json_path) -> Gateway:
    """A single-server shop gateway with no upstream auth configured."""
    return Gateway.from_config(GatewayConfig(servers=[ServerConfig(name='shop', spec=str(shop_json_path))]))


def _expose_tool_and_resource() -> McpIntegration:
    """Build an ``x-mcp-integration`` payload that opts an operation into both tool and resource exposure."""
    return McpIntegration(tool=ToolOverride(), resource=ResourceOverride())


def _operation(operation_id: str, integration: McpIntegration | None = None) -> OperationInfo:
    """Build a minimal ``OperationInfo`` for partition / override tests."""
    return OperationInfo(
        operation_id=operation_id,
        method='get',
        path=f'/{operation_id}',
        x_mcp_integration=integration or McpIntegration(),
    )
