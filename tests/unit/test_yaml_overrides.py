import pytest

from openapi_mcp_gateway.gateway import _apply_yaml_overrides
from openapi_mcp_gateway.openapi import McpIntegration, OperationInfo, ResourceOverride, ToolOverride


def _operation(operation_id: str, integration: McpIntegration | None = None) -> OperationInfo:
    """Build a minimal ``OperationInfo`` for partition / override tests."""
    return OperationInfo(
        operation_id=operation_id,
        method='get',
        path=f'/{operation_id}',
        x_mcp_integration=integration or McpIntegration(),
    )


class TestApplyYamlOverrides:
    """``_apply_yaml_overrides`` swaps in the YAML-side ``x-mcp-integration`` for each named operation."""

    def test_empty_overrides_returns_same_list(self):
        """An empty YAML override map leaves operations untouched."""
        operations = [_operation('get_pet')]
        result = _apply_yaml_overrides(operations, {}, 'petstore')
        assert result is operations

    def test_single_override_applied(self):
        """A YAML override for one op replaces its ``x_mcp_integration``."""
        operations = [_operation('get_pet'), _operation('list_pets')]
        override = McpIntegration(resource=ResourceOverride(name='pet'))
        result = _apply_yaml_overrides(operations, {'get_pet': override}, 'petstore')
        assert result[0].x_mcp_integration is override
        assert result[1].x_mcp_integration is operations[1].x_mcp_integration

    def test_replace_does_not_merge_with_spec(self):
        """YAML override fully replaces spec-side ``x_mcp_integration`` (no merge)."""
        spec_side = McpIntegration(tool=ToolOverride(name='spec_name'))
        operations = [_operation('get_pet', spec_side)]
        yaml_side = McpIntegration(resource=ResourceOverride(name='yaml_name'))
        result = _apply_yaml_overrides(operations, {'get_pet': yaml_side}, 'petstore')
        # YAML wins: resource present, original spec-side tool override gone.
        integration = result[0].x_mcp_integration
        assert integration.tool is None
        assert integration.resource is not None
        assert integration.resource.name == 'yaml_name'

    def test_multiple_overrides_applied(self):
        """Several overrides apply to their respective ops; unmentioned ops untouched."""
        operations = [_operation('a'), _operation('b'), _operation('c')]
        overrides = {
            'a': McpIntegration(resource=ResourceOverride(name='alpha')),
            'c': McpIntegration(tool=ToolOverride(name='gamma')),
        }
        result = _apply_yaml_overrides(operations, overrides, 'srv')
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
        overrides = {'unknown_op': McpIntegration()}
        with pytest.raises(ValueError, match='unknown_op'):
            _apply_yaml_overrides(operations, overrides, 'petstore')

    def test_unmatched_error_lists_all_offending_ids(self):
        """Multiple unmatched op_ids appear in the error message together."""
        operations = [_operation('get_pet')]
        overrides = {'wrong_a': McpIntegration(), 'wrong_b': McpIntegration()}
        with pytest.raises(ValueError, match=r'wrong_a.*wrong_b'):
            _apply_yaml_overrides(operations, overrides, 'petstore')

    def test_does_not_mutate_input_operations(self):
        """The helper returns a new list and does not mutate the input ``OperationInfo`` instances."""
        operations = [_operation('get_pet')]
        original_integration = operations[0].x_mcp_integration
        override = McpIntegration(resource=ResourceOverride(name='pet'))
        _apply_yaml_overrides(operations, {'get_pet': override}, 'petstore')
        assert operations[0].x_mcp_integration is original_integration
