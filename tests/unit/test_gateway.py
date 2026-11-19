import pytest
from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import (
    ResourceGenerator,
    ToolGenerator,
    UpstreamConnection,
)
from openapi_mcp_gateway.gateway import (
    _apply_yaml_overrides,
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
from openapi_mcp_gateway.settings import OperationOverride
from tests.constants import API_URL
from tests.operation import expose_resource


# The securitySchemes of a spec that takes one OAuth token.
OAUTH2_SCHEMES = {'oauth2': {'type': 'oauth2'}}


class TestResourceEligibilityValidation:
    """``_validate_resource_eligibility`` is the strict, fail-fast gate."""

    def test_non_get_rejected(self):
        """``expose.resource`` on a POST raises ``ValueError`` at startup."""
        operation = OperationInfo(
            operation_id='createPet',
            method='post',
            path='/pets',
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
        )
        _validate_resource_eligibility(operation, 'petstore')  # no raise

    def test_uri_template_wrong_scheme_rejected(self):
        """An override URI template that escapes the server's scheme is rejected."""
        operation = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True)],
            x_mcp_integration=expose_resource(uri_template='weather://current/{petId}'),
        )
        with pytest.raises(ValueError, match='must start with "petstore://"'):
            _validate_resource_eligibility(operation, 'petstore')


class TestPartitioning:
    """``_partition_operations`` slots each op into resource / tool / both based on ``promote_resources``."""

    def test_no_opt_in_under_auto_promotes_eligible_get(self):
        """Under ``exposure.promote_resources``, an eligible GET with no opt-in is auto-promoted to a resource."""
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
            x_mcp_integration=expose_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == [op]
        assert tools == []

    def test_tool_only_stays_a_tool(self):
        """``expose.tool`` alone is the existing override path, so op stays a tool."""
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
        """Eligibility validation runs during partition, so non-GET resources fail fast."""
        op = OperationInfo(
            operation_id='create_pet',
            method='post',
            path='/pets',
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
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
            x_mcp_integration=expose_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore')
        assert resources == []
        assert tools == [op]

    def test_auto_skips_ineligible_get(self):
        """Under ``exposure.promote_resources``, a GET with a required non-path parameter stays a tool."""
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
        """Under ``exposure.promote_resources``, a non-GET operation stays a tool."""
        op = OperationInfo(operation_id='create_pet', method='post', path='/pets')
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        assert resources == []
        assert tools == [op]

    def test_auto_respects_explicit_tool_optin(self):
        """Under ``exposure.promote_resources``, an eligible GET with explicit ``expose.tool`` stays a tool.

        Explicit opt-in beats the implicit promotion.
        """
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
        connection = UpstreamConnection(base_url=API_URL)
        op = OperationInfo(
            operation_id='getPet',
            method='get',
            path='/pets/{petId}',
            parameters=[ParameterInfo(name='petId', location='path', required=True, schema={'type': 'string'})],
            x_mcp_integration=_expose_tool_and_resource(),
        )
        resources, tools = _partition_operations([op], 'petstore', promote_resources=True)
        ResourceGenerator(mcp=mcp, connection=connection, server_name='petstore').register(resources)
        ToolGenerator(mcp=mcp, connection=connection).register(tools)

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert 'get_pet' in tool_names

        templates = await mcp.list_resource_templates()
        assert 'petstore://pets/{petId}' in {t.uri_template for t in templates}


class TestApplyYamlOverrides:
    """``_apply_yaml_overrides`` swaps in the YAML-side ``x-mcp-integration`` and ``security`` for each named operation."""

    def test_empty_overrides_returns_same_list(self):
        """An empty YAML override map leaves operations untouched."""
        operations = [_operation('get_pet')]
        result = _apply_yaml_overrides(operations, {}, 'petstore', {})
        assert result is operations

    def test_single_override_applied(self):
        """A YAML override for one op replaces its ``x_mcp_integration``."""
        operations = [_operation('get_pet'), _operation('list_pets')]
        override = OperationOverride(resource=ResourceOverride(name='pet'))
        result = _apply_yaml_overrides(operations, {'get_pet': override}, 'petstore', {})
        assert result[0].x_mcp_integration == McpIntegration(resource=override.resource)
        assert result[1].x_mcp_integration is operations[1].x_mcp_integration

    def test_replace_does_not_merge_with_spec(self):
        """YAML override fully replaces spec-side ``x_mcp_integration`` (no merge)."""
        spec_side = McpIntegration(tool=ToolOverride(name='spec_name'))
        operations = [_operation('get_pet', spec_side)]
        yaml_side = OperationOverride(resource=ResourceOverride(name='yaml_name'))
        result = _apply_yaml_overrides(operations, {'get_pet': yaml_side}, 'petstore', {})
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
        result = _apply_yaml_overrides(operations, overrides, 'srv', {})
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
        operations = [_operation('get_pet')]
        overrides = {'unknown_op': OperationOverride()}
        with pytest.raises(ValueError, match='unknown_op'):
            _apply_yaml_overrides(operations, overrides, 'petstore', {})

    def test_unmatched_error_lists_all_offending_ids(self):
        """Multiple unmatched op_ids appear in the error message together."""
        operations = [_operation('get_pet')]
        overrides = {'wrong_a': OperationOverride(), 'wrong_b': OperationOverride()}
        with pytest.raises(ValueError, match=r'wrong_a.*wrong_b'):
            _apply_yaml_overrides(operations, overrides, 'petstore', {})

    def test_does_not_mutate_input_operations(self):
        """The helper returns a new list and does not mutate the input ``OperationInfo`` instances."""
        operations = [_operation('get_pet')]
        original_integration = operations[0].x_mcp_integration
        override = OperationOverride(resource=ResourceOverride(name='pet'))
        _apply_yaml_overrides(operations, {'get_pet': override}, 'petstore', {})
        assert operations[0].x_mcp_integration is original_integration

    def test_a_security_override_alone_keeps_the_spec_integration(self):
        """Overriding ``security`` replaces the operation's requirements and leaves ``x-mcp-integration`` as it is."""
        spec_side = McpIntegration(tool=ToolOverride(name='spec_name'))
        operations = [_operation('get_pet', spec_side)]
        security = [{'oauth2': ['pets:read']}, {'oauth2': ['pets:admin']}]
        [result] = _apply_yaml_overrides(
            operations, {'get_pet': OperationOverride(security=security)}, 'petstore', OAUTH2_SCHEMES
        )
        assert result.security == security
        assert result.x_mcp_integration is spec_side

    def test_a_security_override_naming_an_undeclared_scheme_raises(self):
        """A misspelt scheme would otherwise count as a credential the gateway lacks, hiding the operation."""
        operations = [_operation('get_pet')]
        override = OperationOverride(security=[{'OAuth2': ['pets:read']}])
        with pytest.raises(ValueError, match=r"operations\.get_pet\.security .* \['OAuth2'\]"):
            _apply_yaml_overrides(operations, {'get_pet': override}, 'petstore', OAUTH2_SCHEMES)


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
