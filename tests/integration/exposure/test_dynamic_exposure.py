import json

import httpx
import pytest
from mcp import Client

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    ExposureConfig,
    GatewayConfig,
    SearchConfig,
    SearchFieldWeights,
    ServerConfig,
)
from tests.gateway import server_mcp
from tests.tool_result import text_of


class TestDynamicExposureEndToEnd:
    """Full assembly chain when ``exposure.style: dynamic`` swaps tools for the three meta-tools."""

    @pytest.fixture
    def dynamic_gateway(self, shop_json_path):
        """Shop gateway with the server flipped to dynamic exposure."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='shop', spec=str(shop_json_path), exposure=ExposureConfig(style='dynamic')),
            ],
        )
        return Gateway.from_config(config)

    async def test_only_three_meta_tools_registered(self, dynamic_gateway):
        """An MCP client sees just ``search_operations``, ``get_operation``, ``call_operation``."""
        async with Client(server_mcp(dynamic_gateway)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {'search_operations', 'get_operation', 'call_operation'}

    async def test_search_field_weights_reach_the_ranking(self, shop_json_path):
        """A weight of 0 on every field but ``path`` ranks by path alone, so a word only in summaries finds nothing."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='shop',
                    spec=str(shop_json_path),
                    exposure=ExposureConfig(
                        style='dynamic',
                        search=SearchConfig(
                            field_weights=SearchFieldWeights(name=0, summary=0, description=0, tags=0, path=1)
                        ),
                    ),
                ),
            ],
        )
        async with Client(server_mcp(Gateway.from_config(config))) as client:
            by_path = (await client.call_tool('search_operations', {'query': 'products'})).structured_content
            by_summary_word = (await client.call_tool('search_operations', {'query': 'list'})).structured_content
        assert by_path is not None and by_path['operations']
        assert by_summary_word is not None and by_summary_word['operations'] == []

    async def test_several_queries_pass_through_an_mcp_client(self, dynamic_gateway):
        """A list of queries passes the advertised schema over MCP, and finds what each phrasing finds."""
        async with Client(server_mcp(dynamic_gateway), raise_exceptions=True) as client:
            single = await client.call_tool('search_operations', {'query': 'products'})
            several = await client.call_tool('search_operations', {'query': ['products', 'xyzzy']})
        assert several.structured_content == single.structured_content
        assert single.structured_content is not None and single.structured_content['operations']

    async def test_list_then_get_then_call_roundtrip(self, dynamic_gateway, mock_upstream):
        """An LLM-style search, get, and call sequence drives an upstream call with correct query."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['method'] = request.method
            captured['url'] = str(request.url)
            return httpx.Response(200, json=[{'id': 1, 'name': 'tote'}])

        mock_upstream(handler)

        async with Client(server_mcp(dynamic_gateway)) as client:
            listing = (await client.call_tool('search_operations', {'query': 'list products'})).structured_content
            assert listing is not None and listing['operations'][0]['name'] == 'list_products'

            described = (await client.call_tool('get_operation', {'name': 'list_products'})).structured_content
            assert described is not None and described['input_schema']['properties']['limit']['type'] == 'integer'

            result = await client.call_tool('call_operation', {'name': 'list_products', 'arguments': {'limit': 5}})
        assert captured['method'] == 'GET'
        assert 'limit=5' in captured['url']
        assert result.is_error is False
        assert json.loads(text_of(result)) == [{'id': 1, 'name': 'tote'}]
