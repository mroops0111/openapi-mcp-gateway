import pytest

from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.policy import filter_operations, matches_pattern


def _operation(operation_id: str, method: str = 'get', path: str = '/test', mcp_marker: bool = False) -> OperationInfo:
    """Build a minimal ``OperationInfo`` for pattern/filter tests."""
    return OperationInfo(
        operation_id=operation_id,
        method=method,
        path=path,
        x_mcp_integration={'expose': {'tool': {}}} if mcp_marker else {},
    )


OPERATIONS = [
    _operation('listPets', 'get', '/pets'),
    _operation('createPet', 'post', '/pets'),
    _operation('getPetById', 'get', '/pets/{petId}'),
    _operation('deletePet', 'delete', '/pets/{petId}'),
    _operation('listUsers', 'get', '/users'),
    _operation('adminListPets', 'get', '/admin/pets', mcp_marker=True),
]


class TestMatchesPattern:
    """Pattern matching against operation id and ``METHOD /path`` syntax."""

    def test_exact_operation_id(self):
        """An exact operation id matches only that operation."""
        assert matches_pattern(OPERATIONS[0], 'listPets') is True
        assert matches_pattern(OPERATIONS[0], 'createPet') is False

    def test_wildcard_operation_id(self):
        """``*`` wildcards match operation id substrings."""
        assert matches_pattern(OPERATIONS[0], 'list*') is True
        assert matches_pattern(OPERATIONS[0], '*Pets') is True
        assert matches_pattern(OPERATIONS[0], '*User*') is False

    def test_method_path_exact(self):
        """``METHOD /path`` matches when both method and path are exact."""
        assert matches_pattern(OPERATIONS[0], 'GET /pets') is True
        assert matches_pattern(OPERATIONS[0], 'POST /pets') is False

    def test_method_path_wildcard(self):
        """Wildcards inside ``METHOD /path`` cover sub-paths."""
        assert matches_pattern(OPERATIONS[2], 'GET /pets/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE /pets/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE *') is True

    def test_method_case_insensitive(self):
        """The HTTP method is matched case-insensitively."""
        assert matches_pattern(OPERATIONS[0], 'get /pets') is True
        assert matches_pattern(OPERATIONS[0], 'Get /pets') is True

    def test_path_case_sensitive(self):
        """The path component is matched case-sensitively."""
        assert matches_pattern(OPERATIONS[0], 'GET /Pets') is False


class TestFilterOperations:
    """Allow/deny/marked-only combinations on ``filter_operations``."""

    def test_no_filters(self):
        """No filters returns every operation."""
        result = filter_operations(OPERATIONS)
        assert len(result) == len(OPERATIONS)

    @pytest.mark.parametrize(
        ('allow', 'expected'),
        [
            (['GET /pets/*'], ['getPetById']),
            (['GET /pets', 'GET /users'], ['listPets', 'listUsers']),
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
        assert 'deletePet' not in ids
        assert 'listPets' in ids

    def test_allow_and_deny(self):
        """``deny`` is applied after ``allow``."""
        result = filter_operations(OPERATIONS, allow=['*Pet*'], deny=['DELETE *'])
        ids = [op.operation_id for op in result]
        assert 'listPets' in ids
        assert 'createPet' in ids
        assert 'deletePet' not in ids

    def test_marked_only(self):
        """``marked_only`` keeps only operations with the ``x-mcp`` expose marker."""
        result = filter_operations(OPERATIONS, marked_only=True)
        assert len(result) == 1
        assert result[0].operation_id == 'adminListPets'

    def test_marked_only_with_allow(self):
        """``marked_only`` and ``allow`` intersect, not union."""
        result = filter_operations(OPERATIONS, allow=['admin*'], marked_only=True)
        assert len(result) == 1

    def test_allow_matches_nothing(self):
        """An ``allow`` that matches no operation yields an empty list."""
        result = filter_operations(OPERATIONS, allow=['nonexistent*'])
        assert result == []
