import json
import typing

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from openapi_mcp_gateway.exposure import (
    MetaToolGenerator,
    UpstreamConnection,
)
from openapi_mcp_gateway.openapi import (
    OperationInfo,
    ParameterInfo,
)
from openapi_mcp_gateway.scopes import UpstreamNoGrantSource
from tests.constants import API_URL
from tests.operation import shaped_operation
from tests.served_tool import ServedTool, tool_names
from tests.tool_result import text_of


class TestMetaToolRegistration:
    """``MetaToolGenerator.register`` produces exactly three meta-tools regardless of N operations."""

    async def test_exactly_three_tools_registered(self):
        """Three operations in, three meta-tools out, namely search, get, and call."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        names = set(await tool_names(mcp))
        assert names == {'search_operations', 'get_operation', 'call_operation'}

    async def test_empty_ops_still_registers_three(self):
        """No operations still produces three meta-tools (callable on an empty registry)."""
        generator, mcp = _meta_generator()
        generator.register([])
        names = set(await tool_names(mcp))
        assert names == {'search_operations', 'get_operation', 'call_operation'}


class TestSearchOperations:
    """``search_operations`` ranks operations against a query, filters by tag, and pages the result."""

    async def test_no_arguments_lists_everything_with_tag_counts(self):
        """With neither query nor tags, every operation is listed, and the first page adds the tags and their counts."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        assert await _search(mcp) == {
            'operations': [
                {'name': 'list_products', 'description': 'List products.'},
                {'name': 'buy_product', 'description': 'Buy a product.'},
                {'name': 'list_orders', 'description': 'List orders.'},
            ],
            'tags': [{'name': 'products', 'operations': 2}, {'name': 'store', 'operations': 1}],
        }

    async def test_tag_counts_only_on_the_first_page(self):
        """Later pages of the full listing leave the tag counts out."""
        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(operation_id=f'listThing{index}', method='get', path=f'/things/{index}', tags=['things'])
                for index in range(25)
            ]
        )
        first = await _search(mcp)
        assert first['tags'] == [{'name': 'things', 'operations': 25}]
        second = await _search(mcp, cursor=first['nextCursor'])
        assert 'tags' not in second

    async def test_no_arguments_without_tags_lists_operations(self):
        """A spec without tags lists every operation by name and description, with no tag counts."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        assert await _search(mcp) == {
            'operations': [
                {'name': 'list_products', 'description': 'List all products.'},
                {'name': 'get_product_by_id', 'description': 'Get one product.'},
            ]
        }

    async def test_falls_back_to_method_path_when_no_text(self):
        """An op without description or summary falls back to ``METHOD /path``."""
        generator, mcp = _meta_generator()
        generator.register([OperationInfo(operation_id='ping', method='get', path='/ping')])
        assert await _search(mcp) == {'operations': [{'name': 'ping', 'description': 'GET /ping'}]}

    async def test_query_ranks_matches_and_leaves_out_the_rest(self):
        """The best match comes first, and an operation sharing no term with the query is left out."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        payload = await _search(mcp, query='buy a product')
        assert [entry['name'] for entry in payload['operations']] == ['buy_product', 'list_products']

    async def test_tags_filter_without_query(self):
        """Tags alone list the operations carrying any of them, matched without regard to case."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        payload = await _search(mcp, tags=['Store'])
        assert [entry['name'] for entry in payload['operations']] == ['list_orders']

    async def test_tags_filter_narrows_query(self):
        """Tags restrict a query to the operations carrying them."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        payload = await _search(mcp, query='list', tags=['store'])
        assert [entry['name'] for entry in payload['operations']] == ['list_orders']

    async def test_pages_with_next_cursor(self):
        """A result longer than one page carries ``nextCursor``, and the last page does not."""
        generator, mcp = _meta_generator()
        generator.register(_many_things())
        first = await _search(mcp, query='list things')
        assert len(first['operations']) == 20
        second = await _search(mcp, query='list things', cursor=first['nextCursor'])
        assert len(second['operations']) == 5
        assert 'nextCursor' not in second
        names = [entry['name'] for entry in first['operations'] + second['operations']]
        assert len(set(names)) == 25

    async def test_rejects_a_cursor_it_did_not_issue(self):
        """A cursor that does not decode to an offset raises."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        with pytest.raises(UnexpectedToolError) as raised:
            await _search(mcp, query='products', cursor='not-a-cursor')
        assert isinstance(raised.value.__cause__, ValueError)
        assert 'Invalid cursor' in str(raised.value.__cause__)

    async def test_blank_query_filters_nothing(self):
        """A query of only whitespace is treated as omitted and lists everything."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        assert await _search(mcp, query='   ') == await _search(mcp)

    async def test_tag_counts_merge_spellings_that_differ_in_case(self):
        """Tags differing only in case count as one, in the spelling seen first, matching how filtering treats them."""
        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(operation_id='listProducts', method='get', path='/products', tags=['Products']),
                OperationInfo(operation_id='buyProduct', method='post', path='/products/buy', tags=['products']),
            ]
        )
        payload = await _search(mcp)
        assert payload['tags'] == [{'name': 'Products', 'operations': 2}]
        assert len((await _search(mcp, tags=['PRODUCTS']))['operations']) == 2

    async def test_description_cut_to_first_paragraph(self):
        """A result shows only the first paragraph of a long description, capped in length."""
        generator, mcp = _meta_generator()
        long_first_paragraph = 'Merge a pull request. ' * 20
        generator.register(
            [
                OperationInfo(
                    operation_id='pullsMerge',
                    method='put',
                    path='/pulls/{pull_number}/merge',
                    description='Merge it.\n\nMore detail here.',
                ),
                OperationInfo(
                    operation_id='pullsMergeLong',
                    method='put',
                    path='/pulls/merge',
                    description=long_first_paragraph,
                ),
            ]
        )
        payload = await _search(mcp, query='merge')
        descriptions = {entry['name']: entry['description'] for entry in payload['operations']}
        assert descriptions['pulls_merge'] == 'Merge it.'
        assert len(descriptions['pulls_merge_long']) == 200
        assert descriptions['pulls_merge_long'].endswith('…')

    async def test_several_queries_merge_their_matches(self):
        """Each phrasing finds its own match, and an operation both find ranks first."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        payload = await _search(mcp, query=['buy', 'orders'])
        assert [entry['name'] for entry in payload['operations']] == ['buy_product', 'list_orders']
        payload = await _search(mcp, query=['list products', 'products'])
        assert payload['operations'][0]['name'] == 'list_products'

    async def test_one_query_in_a_list_ranks_as_a_string(self):
        """A list holding one query, repeated or padded with blanks, ranks exactly as that query alone."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        assert await _search(mcp, query=['buy a product', ' ', 'buy a product']) == await _search(
            mcp, query='buy a product'
        )

    async def test_blank_queries_filter_nothing(self):
        """A list of only blank queries is treated as omitted and lists everything."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        assert await _search(mcp, query=['', '  ']) == await _search(mcp)
        assert await _search(mcp, query=[]) == await _search(mcp)

    async def test_query_schema_takes_a_string_or_a_list(self):
        """The advertised schema lets a model pass one query or several."""
        generator, mcp = _meta_generator()
        generator.register(_tagged_ops())
        tool = ServedTool(mcp, 'search_operations')
        branches = (await tool.input_schema())['properties']['query']['anyOf']
        assert {'type': 'string'} in branches
        assert {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 10} in branches

    async def test_parameters_are_searched_when_weighted(self):
        """An argument's name and description reach search once the parameters field has a weight, and not before."""
        operation = OperationInfo(
            operation_id='updateIssue',
            method='patch',
            path='/issues/{number}',
            summary='Update an issue.',
            parameters=[
                ParameterInfo(name='state', location='body', description='Set to closed to close the issue.'),
            ],
        )
        generator, mcp = _meta_generator()
        generator.register([operation])
        assert await _search(mcp, query='closed') == {'operations': []}
        generator, mcp = _meta_generator(field_weight_overrides={'parameters': 1.0})
        generator.register([operation])
        assert [entry['name'] for entry in (await _search(mcp, query='closed'))['operations']] == ['update_issue']


class TestGetOperation:
    """``get_operation`` returns the input schema for one operation, and unknown names raise."""

    async def test_known_returns_full_schema(self):
        """A known name yields ``{name, description, input_schema}`` with the JSON Schema for inputs."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = ServedTool(mcp, 'get_operation')
        payload = (await tool.call({'name': 'get_product_by_id'})).structured_content
        assert payload['name'] == 'get_product_by_id'
        assert payload['description'] == 'Get one product.'
        schema = payload['input_schema']
        assert schema['type'] == 'object'
        assert schema['properties']['productId']['type'] == 'integer'
        assert schema['required'] == ['productId']

    async def test_unknown_raises(self):
        """An unknown name raises ``ValueError`` so MCPServer surfaces it as a tool error."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = ServedTool(mcp, 'get_operation')
        with pytest.raises(UnexpectedToolError) as raised:
            await tool.call({'name': 'no_such_op'})
        assert isinstance(raised.value.__cause__, ValueError)
        assert 'Unknown operation' in str(raised.value.__cause__)

    async def test_no_required_key_when_all_optional(self):
        """When no parameter is required, the schema omits the ``required`` array entirely."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = ServedTool(mcp, 'get_operation')
        payload = (await tool.call({'name': 'list_products'})).structured_content
        assert 'required' not in payload['input_schema']

    async def test_param_description_propagates(self):
        """``ParameterInfo.description`` lands on the JSON Schema property."""
        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(
                    operation_id='listProducts',
                    method='get',
                    path='/products',
                    parameters=[
                        ParameterInfo(
                            name='limit',
                            location='query',
                            required=False,
                            description='Max items.',
                            schema={'type': 'integer'},
                        ),
                    ],
                )
            ]
        )
        tool = ServedTool(mcp, 'get_operation')
        payload = (await tool.call({'name': 'list_products'})).structured_content
        assert payload['input_schema']['properties']['limit']['description'] == 'Max items.'


class TestCallOperation:
    """``call_operation`` dispatches to the underlying HTTP callable, and unknown names raise."""

    async def test_dispatches_to_upstream(self, mock_upstream):
        """Calling a known op hits the upstream with the resolved path and query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'id': 7, 'name': 'rex'})

        mock_upstream(handler)

        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = ServedTool(mcp, 'call_operation')

        result = await tool.call({'name': 'get_product_by_id', 'arguments': {'productId': 7}})
        assert captured['method'] == 'GET'
        assert captured['url'].endswith('/products/7')
        assert result.is_error is False
        assert result.structured_content == {'id': 7, 'name': 'rex'}
        assert json.loads(text_of(result)) == {'id': 7, 'name': 'rex'}

    async def test_unknown_raises(self):
        """An unknown name raises ``ValueError`` without contacting upstream."""
        generator, mcp = _meta_generator()
        generator.register(_ops())
        tool = ServedTool(mcp, 'call_operation')
        with pytest.raises(UnexpectedToolError) as raised:
            await tool.call({'name': 'no_such_op', 'arguments': {}})
        assert isinstance(raised.value.__cause__, ValueError)
        assert 'Unknown operation' in str(raised.value.__cause__)

    async def test_sanitised_arg_name_reaches_upstream(self, mock_upstream):
        """Dashed query params are exposed with sanitised keys, while upstream sees the original name."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'ok': True})

        mock_upstream(handler)

        generator, mcp = _meta_generator()
        generator.register(
            [
                OperationInfo(
                    operation_id='getThing',
                    method='get',
                    path='/things',
                    parameters=[
                        ParameterInfo(
                            name='enterprise-team',
                            location='query',
                            required=True,
                            schema={'type': 'string'},
                        ),
                    ],
                )
            ]
        )
        tool = ServedTool(mcp, 'call_operation')
        await tool.call({'name': 'get_thing', 'arguments': {'enterprise_team': 'acme'}})
        assert 'enterprise-team=acme' in captured['url']


class TestDynamicParamShaping:
    """Dynamic (meta-tool) exposure applies the same shaping as the static path."""

    async def test_get_operation_schema_omits_hidden(self):
        """``get_operation`` advertises the shaped schema, without hidden params."""
        op = shaped_operation(
            [
                ParameterInfo(name='secret', location='query', required=True, schema={'type': 'string'}),
                ParameterInfo(name='visible', location='query', required=True, schema={'type': 'string'}),
            ],
            params={'secret': {'hidden': True}},
        )
        generator, mcp = _meta_generator()
        generator.register([op])
        get_operation = ServedTool(mcp, 'get_operation')
        payload = (await get_operation.call({'name': 'do_thing'})).structured_content
        schema = payload['input_schema']
        assert 'secret' not in schema['properties']
        assert 'visible' in schema['properties']

    async def test_request_transform_applies_in_dynamic_mode(self, mock_upstream):
        """A request expression shapes the upstream call through the dynamic call_operation path too."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [
                ParameterInfo(
                    name='status', location='query', required=True, schema={'type': 'string', 'enum': ['open']}
                )
            ],
            request='{"op[status_id]": $lookup({"open": "o"}, status)}',
        )
        generator, mcp = _meta_generator()
        generator.register([op])
        call_operation = ServedTool(mcp, 'call_operation')
        await call_operation.call({'name': 'do_thing', 'arguments': {'status': 'open'}})
        assert captured['params']['op[status_id]'] == 'o'


def _meta_generator(**options: typing.Any) -> tuple[MetaToolGenerator, MCPServer]:
    """Build a fresh MetaToolGenerator + MCPServer pair for each test, with ``options`` passed on."""
    mcp = MCPServer('test')
    generator = MetaToolGenerator(
        mcp=mcp, connection=UpstreamConnection(base_url=API_URL), grant_source=UpstreamNoGrantSource(), **options
    )
    return generator, mcp


def _ops() -> list[OperationInfo]:
    """Two operations covering required path param + optional query param."""
    return [
        OperationInfo(
            operation_id='listProducts',
            method='get',
            path='/products',
            description='List all products.',
            parameters=[
                ParameterInfo(name='limit', location='query', required=False, schema={'type': 'integer'}),
            ],
        ),
        OperationInfo(
            operation_id='getProductById',
            method='get',
            path='/products/{productId}',
            summary='Get one product.',
            parameters=[
                ParameterInfo(name='productId', location='path', required=True, schema={'type': 'integer'}),
            ],
        ),
    ]


async def _search(mcp: MCPServer, **arguments: typing.Any) -> dict[str, typing.Any]:
    """Call ``search_operations`` with ``arguments`` and return its structured payload."""
    tool = ServedTool(mcp, 'search_operations')
    return (await tool.call(arguments)).structured_content


def _tagged_ops() -> list[OperationInfo]:
    """Two product operations and one store operation."""
    return [
        OperationInfo(
            operation_id='listProducts', method='get', path='/products', summary='List products.', tags=['products']
        ),
        OperationInfo(
            operation_id='buyProduct',
            method='post',
            path='/products/{productId}/buy',
            summary='Buy a product.',
            tags=['products'],
        ),
        OperationInfo(operation_id='listOrders', method='get', path='/orders', summary='List orders.', tags=['store']),
    ]


def _many_things() -> list[OperationInfo]:
    """Twenty-five untagged operations, more than one search page holds."""
    return [
        OperationInfo(operation_id=f'listThing{index}', method='get', path=f'/things/{index}') for index in range(25)
    ]
