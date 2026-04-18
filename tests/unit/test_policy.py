"""Tests for policy filtering."""

from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.policy import filter_operations, matches_pattern


def _op(operation_id: str, method: str = 'get', path: str = '/test', mcp_marker: bool = False) -> OperationInfo:
    return OperationInfo(
        operation_id=operation_id,
        method=method,
        path=path,
        x_mcp_integration={'expose': {'tool': {}}} if mcp_marker else {},
    )


OPERATIONS = [
    _op('listPets', 'get', '/pets'),
    _op('createPet', 'post', '/pets'),
    _op('getPetById', 'get', '/pets/{petId}'),
    _op('deletePet', 'delete', '/pets/{petId}'),
    _op('listUsers', 'get', '/users'),
    _op('adminListPets', 'get', '/admin/pets', mcp_marker=True),
]


class TestMatchesPattern:
    def test_exact_operation_id(self):
        assert matches_pattern(OPERATIONS[0], 'listPets') is True
        assert matches_pattern(OPERATIONS[0], 'createPet') is False

    def test_wildcard_operation_id(self):
        assert matches_pattern(OPERATIONS[0], 'list*') is True
        assert matches_pattern(OPERATIONS[0], '*Pets') is True
        assert matches_pattern(OPERATIONS[0], '*User*') is False

    def test_method_path_exact(self):
        assert matches_pattern(OPERATIONS[0], 'GET /pets') is True
        assert matches_pattern(OPERATIONS[0], 'POST /pets') is False

    def test_method_path_wildcard(self):
        assert matches_pattern(OPERATIONS[2], 'GET /pets/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE /pets/*') is True
        assert matches_pattern(OPERATIONS[3], 'DELETE *') is True

    def test_method_case_insensitive(self):
        assert matches_pattern(OPERATIONS[0], 'get /pets') is True
        assert matches_pattern(OPERATIONS[0], 'Get /pets') is True

    def test_path_case_sensitive(self):
        assert matches_pattern(OPERATIONS[0], 'GET /Pets') is False


class TestFilterOperations:
    def test_no_filters(self):
        result = filter_operations(OPERATIONS)
        assert len(result) == len(OPERATIONS)

    def test_allow(self):
        result = filter_operations(OPERATIONS, allow=['GET /pets/*'])
        ids = [op.operation_id for op in result]
        assert ids == ['getPetById']

    def test_allow_multiple(self):
        result = filter_operations(OPERATIONS, allow=['GET /pets', 'GET /users'])
        ids = [op.operation_id for op in result]
        assert 'listPets' in ids
        assert 'listUsers' in ids

    def test_deny(self):
        result = filter_operations(OPERATIONS, deny=['DELETE *'])
        ids = [op.operation_id for op in result]
        assert 'deletePet' not in ids
        assert 'listPets' in ids

    def test_allow_and_deny(self):
        result = filter_operations(OPERATIONS, allow=['*Pet*'], deny=['DELETE *'])
        ids = [op.operation_id for op in result]
        assert 'listPets' in ids
        assert 'createPet' in ids
        assert 'deletePet' not in ids

    def test_marked_only(self):
        result = filter_operations(OPERATIONS, marked_only=True)
        assert len(result) == 1
        assert result[0].operation_id == 'adminListPets'

    def test_marked_only_with_allow(self):
        result = filter_operations(OPERATIONS, allow=['admin*'], marked_only=True)
        assert len(result) == 1

    def test_allow_matches_nothing(self):
        result = filter_operations(OPERATIONS, allow=['nonexistent*'])
        assert result == []
