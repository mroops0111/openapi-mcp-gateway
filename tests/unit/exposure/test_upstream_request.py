import json

import pytest

from openapi_mcp_gateway.openapi import OperationInfo, ParameterInfo
from tests.context import stub_context
from tests.operation import query_parameter, register_tool, shaped_operation


class TestQuerySerialization:
    """The query string follows each spec query parameter's ``explode``, whichever way the request is built."""

    @staticmethod
    async def _sent_query(upstream_requests, operation: OperationInfo, arguments: dict) -> str:
        """Call ``operation`` as a registered tool with ``arguments`` and return the query string it sent."""
        result = await register_tool(operation).run(arguments, context=stub_context())
        assert result.is_error is False
        return upstream_requests[-1].url.query.decode()

    @pytest.mark.parametrize(
        ('explode', 'expected'),
        [(False, 'opt_fields=name,due_on'), (True, 'opt_fields=name&opt_fields=due_on')],
    )
    async def test_arguments_follow_explode(self, upstream_requests, explode, expected):
        """An argument passed through is sent in the shape its parameter's ``explode`` declares."""
        operation = shaped_operation([query_parameter(explode=explode)])
        assert await self._sent_query(upstream_requests, operation, {'opt_fields': ['name', 'due_on']}) == expected

    async def test_replaced_surface_still_follows_the_spec_parameter(self, upstream_requests):
        """Under ``replace``, a request key naming a spec query parameter is sent as that parameter declares."""
        operation = shaped_operation(
            [query_parameter(explode=False)],
            params={'fields': {'type': 'array', 'items': {'type': 'string'}}},
            params_strategy='replace',
            request='{"opt_fields": fields}',
        )
        assert await self._sent_query(upstream_requests, operation, {'fields': ['name', 'gid']}) == (
            'opt_fields=name,gid'
        )

    async def test_forwarded_arguments_go_out_under_their_spec_name(self, upstream_requests):
        """A request expression forwarding ``$`` sends a sanitised argument name as the spec names it."""
        operation = shaped_operation([query_parameter('fields[account]', explode=False)], request='$')
        query = await self._sent_query(upstream_requests, operation, {'fields_account_': ['a', 'b']})
        assert query == 'fields%5Baccount%5D=a,b'

    async def test_query_parameter_of_a_body_method_stays_in_the_query(self, upstream_requests):
        """On a POST, a request key naming a query parameter goes to the query string, and the rest to the body."""
        operation = shaped_operation(
            [query_parameter(explode=False), ParameterInfo(name='name', location='body', schema={'type': 'string'})],
            method='post',
            request='$',
        )
        query = await self._sent_query(upstream_requests, operation, {'opt_fields': ['a', 'b'], 'name': 'n'})
        assert query == 'opt_fields=a,b'
        assert json.loads(upstream_requests[-1].content) == {'name': 'n'}

    async def test_undeclared_object_keeps_its_key(self, upstream_requests):
        """An object under a request key no parameter declares is sent whole as JSON under that key."""
        operation = shaped_operation(
            [query_parameter('q', schema={'type': 'string'})], request='{"q": q, "created": {"gt": 5}}'
        )
        assert await self._sent_query(upstream_requests, operation, {'q': 'x'}) == 'q=x&created=%7B%22gt%22%3A5%7D'

    async def test_object_property_cannot_override_another_parameter(self, upstream_requests):
        """An exploded object property naming another query parameter, hidden or not, is refused before any call."""
        filter_parameter = query_parameter('filter', schema={'type': 'object'})
        format_parameter = query_parameter('format', schema={'type': 'string', 'default': 'json'})
        operation = shaped_operation(
            [filter_parameter, format_parameter], params={'format': {'hidden': True, 'default': 'json'}}
        )
        result = await register_tool(operation).run(
            {'filter': {'format': 'xml', 'color': 'red'}}, context=stub_context()
        )
        assert result.is_error is True
        assert 'filter.format' in result.content[0].text
        assert upstream_requests == []
