import pytest

from openapi_mcp_gateway.openapi import McpIntegration, OperationInfo, ToolOverride
from openapi_mcp_gateway.policy import filter_operations, matches_pattern


class TestMatchesPattern:
    """Pattern matching against operation id and ``METHOD /path`` syntax."""

    def test_exact_operation_id(self):
        """An exact operation id matches only that operation."""
        assert matches_pattern(OPERATIONS[0], 'listProducts') is True
        assert matches_pattern(OPERATIONS[0], 'createProduct') is False

    def test_wildcard_operation_id(self):
        """``*`` wildcards match operation id substrings."""
        assert matches_pattern(OPERATIONS[0], 'list*') is True
        assert matches_pattern(OPERATIONS[0], '*Products') is True
        assert matches_pattern(OPERATIONS[0], '*User*') is False

    def test_method_path_exact(self):
        """``METHOD /path`` matches when both method and path are exact."""
        assert matches_pattern(OPERATIONS[0], 'GET /products') is True
        assert matches_pattern(OPERATIONS[0], 'POST /products') is False

    def test_method_path_wildcard(self):
        """Wildcards inside ``METHOD /path`` cover sub-paths."""
        assert matches_pattern(OPERATIONS[2], 'GET /products/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE /products/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE *') is True

    def test_method_case_insensitive(self):
        """The HTTP method is matched case-insensitively."""
        assert matches_pattern(OPERATIONS[0], 'get /products') is True
        assert matches_pattern(OPERATIONS[0], 'Get /products') is True

    def test_path_case_sensitive(self):
        """The path component is matched case-sensitively."""
        assert matches_pattern(OPERATIONS[0], 'GET /Products') is False


class TestFilterOperations:
    """Allow/deny/marked-only combinations on ``filter_operations``."""

    def test_no_filters(self):
        """No filters returns every operation."""
        result = filter_operations(OPERATIONS)
        assert len(result) == len(OPERATIONS)

    @pytest.mark.parametrize(
        ('allow', 'expected'),
        [
            (['GET /products/*'], ['getProductById']),
            (['GET /products', 'GET /users'], ['listProducts', 'listUsers']),
        ],
    )
    def test_allow(self, allow, expected):
        """``allow`` keeps only operations matching at least one pattern."""
        result = filter_operations(OPERATIONS, allow=allow)
        ids = [op.operation_id for op in result]
        assert sorted(ids) == sorted(expected)

    def test_deny(self):
        """``deny`` drops operations matching any pattern."""
        result = filter_operations(OPERATIONS, deny=['DELETE *'])
        ids = [op.operation_id for op in result]
        assert 'deleteProduct' not in ids
        assert 'listProducts' in ids

    def test_allow_and_deny(self):
        """``deny`` is applied after ``allow``."""
        result = filter_operations(OPERATIONS, allow=['*Product*'], deny=['DELETE *'])
        ids = [op.operation_id for op in result]
        assert 'listProducts' in ids
        assert 'createProduct' in ids
        assert 'deleteProduct' not in ids

    def test_annotated_only(self):
        """``annotated_only`` keeps only operations with the ``x-mcp`` expose marker."""
        result = filter_operations(OPERATIONS, annotated_only=True)
        assert len(result) == 1
        assert result[0].operation_id == 'adminListProducts'

    def test_annotated_only_with_allow(self):
        """``annotated_only`` and ``allow`` intersect, not union."""
        # ``listProducts`` matches the pattern but carries no marker, so a union would keep it.
        result = filter_operations(OPERATIONS, allow=['*Products'], annotated_only=True)
        assert [operation.operation_id for operation in result] == ['adminListProducts']

    def test_allow_matches_nothing(self):
        """An ``allow`` that matches no operation yields an empty list."""
        result = filter_operations(OPERATIONS, allow=['nonexistent*'])
        assert result == []


def _operation(operation_id: str, method: str = 'get', path: str = '/test', mcp_marker: bool = False) -> OperationInfo:
    """Build a minimal ``OperationInfo`` for pattern/filter tests."""
    integration = McpIntegration(tool=ToolOverride()) if mcp_marker else McpIntegration()
    return OperationInfo(
        operation_id=operation_id,
        method=method,
        path=path,
        x_mcp_integration=integration,
    )


# The operations every pattern and filter test runs against, one of them marked for exposure.
OPERATIONS = [
    _operation('listProducts', 'get', '/products'),
    _operation('createProduct', 'post', '/products'),
    _operation('getProductById', 'get', '/products/{productId}'),
    _operation('deleteProduct', 'delete', '/products/{productId}'),
    _operation('listUsers', 'get', '/users'),
    _operation('adminListProducts', 'get', '/admin/products', mcp_marker=True),
]
