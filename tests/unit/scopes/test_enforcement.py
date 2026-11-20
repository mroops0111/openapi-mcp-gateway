import pytest

from openapi_mcp_gateway.exposure import DynamicOperationTarget
from openapi_mcp_gateway.scopes import (
    NameHeaderTarget,
)


class TestRequestTargets:
    """Which name a 2026-07-28 request reaches, read from its headers alone."""

    @pytest.mark.parametrize('name', ['delete_pet', 'petstore://pets/7'])
    def test_the_name_header_names_a_tool_or_a_resource(self, name):
        """``Mcp-Name`` carries the tool of a ``tools/call`` and the URI of a ``resources/read``."""
        assert NameHeaderTarget().resolve({'mcp-name': name}) == name

    @pytest.mark.parametrize(
        ('headers', 'expected'),
        [
            ({'mcp-name': 'call_operation', 'mcp-param-operation': 'delete_pet'}, 'delete_pet'),
            ({'mcp-name': 'search_operations'}, None),
            ({'mcp-name': 'call_operation'}, None),
        ],
    )
    def test_dynamic_exposure_reads_the_operation_call_operation_names(self, headers, expected):
        """Only ``call_operation`` reaches a single operation, named in its ``Mcp-Param-Operation`` header."""
        assert DynamicOperationTarget().resolve(headers) == expected
