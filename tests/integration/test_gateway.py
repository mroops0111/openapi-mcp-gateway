import pytest
from mcp import Client

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    GatewayConfig,
    ServerConfig,
)
from tests.gateway import connect_in_memory, tool_names


class TestHealthEndpoint:
    """``/healthz`` reports overall status and per-server auth mode."""

    def test_healthz(self, client):
        """Endpoint reports ``status: ok`` and lists every registered server."""
        response = client.get('/healthz')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        assert len(data['servers']) == 1
        assert data['servers'][0]['name'] == 'shop'
        assert data['servers'][0]['auth'] == {'mcp': 'open', 'upstream': 'none'}


class TestInstructions:
    """A server's ``instructions`` reach the client in the ``initialize`` result."""

    @pytest.mark.parametrize('instructions', ['Use get_product_by_id before listing products.', None])
    async def test_initialize_carries_the_configured_instructions(self, shop_json_path, instructions):
        """Set instructions arrive verbatim, and unset ones leave the field out."""
        gateway = Gateway()
        gateway.add_server(name='products', spec=str(shop_json_path), instructions=instructions)

        async with connect_in_memory(gateway) as mcp_client:
            assert mcp_client.instructions == instructions


class TestSpec20260728Adoption:
    """Cache hints and stable ordering, adopted from the 2026-07-28 spec on top of the v2 SDK."""

    async def test_static_lists_carry_cache_hints(self, gateway):
        """``tools/list`` advertises the gateway's public, minutes-long freshness hint."""
        bundle = gateway.describe_servers()[0]
        async with Client(bundle.mcp) as mcp_client:
            result = await mcp_client.list_tools()
        dumped = result.model_dump(by_alias=True)
        assert dumped['ttlMs'] == 300_000
        assert dumped['cacheScope'] == 'public'

    async def test_tool_registration_order_is_deterministic(self, shop_json_path):
        """Two gateways built from the same spec register tools in the same order."""

        async def registered() -> list[str]:
            config = GatewayConfig(servers=[ServerConfig(name='shop', spec=str(shop_json_path))])
            return await tool_names(Gateway.from_config(config))

        first = await registered()
        assert first, 'shop should register at least one tool'
        assert first == await registered()
