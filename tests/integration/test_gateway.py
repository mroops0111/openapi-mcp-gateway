import pytest
from mcp import Client

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    GatewayConfig,
    PolicyConfig,
    ServerConfig,
)


class TestGatewayAssembly:
    """Server registration, spec parsing, auth provider wiring, and policy validation."""

    def test_servers_registered(self, gateway):
        """A configured server appears in ``_servers`` with its derived mount path."""
        assert len(gateway._servers) == 1
        assert gateway._servers[0].name == 'petstore'
        assert gateway._servers[0].mount_path == '/petstore'

    def test_spec_parsed(self, gateway):
        """The OpenAPI spec is parsed and its operations are discoverable."""
        spec = gateway._servers[0].spec
        assert spec.title == 'Petstore'
        ids = [op.operation_id for op in spec.operations]
        assert 'listPets' in ids

    def test_no_auth_provider_for_no_auth(self, gateway):
        """A server without auth config has no ``auth_provider`` attached."""
        assert gateway._servers[0].auth_provider is None

    def test_multiple_servers(self, petstore_json_path):
        """Multiple servers register on independent mount paths."""
        config = GatewayConfig(
            servers=[
                ServerConfig(name='pets', spec=str(petstore_json_path)),
                ServerConfig(name='pets2', spec=str(petstore_json_path), path_prefix='other'),
            ],
        )
        gateway = Gateway.from_config(config)
        assert len(gateway._servers) == 2
        paths = [server.mount_path for server in gateway._servers]
        assert '/pets' in paths
        assert '/other' in paths

    def test_empty_operations_raises(self, petstore_json_path):
        """A policy that filters every operation out fails fast at assembly time."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='test',
                    spec=str(petstore_json_path),
                    policy=PolicyConfig(allow=['NONEXISTENT_OPERATION']),
                ),
            ],
        )
        with pytest.raises(ValueError):
            Gateway.from_config(config)


class TestHealthEndpoint:
    """``/healthz`` reports overall status and per-server auth mode."""

    def test_healthz(self, client):
        """Endpoint reports ``status: ok`` and lists every registered server."""
        response = client.get('/healthz')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        assert len(data['servers']) == 1
        assert data['servers'][0]['name'] == 'petstore'
        assert data['servers'][0]['auth'] == 'static'


class TestInstructions:
    """A server's ``instructions`` reach the client in the ``initialize`` result."""

    @pytest.mark.parametrize('instructions', ['Use get_pet_by_id before listing pets.', None])
    async def test_initialize_carries_the_configured_instructions(self, petstore_json_path, instructions):
        """Set instructions arrive verbatim, and unset ones leave the field out."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(petstore_json_path), instructions=instructions)

        async with Client(gateway.describe_servers()[0].mcp) as mcp_client:
            assert mcp_client.instructions == instructions


class TestSpec20260728Adoption:
    """Cache hints and stable ordering, adopted from the 2026-07-28 spec on top of the v2 SDK."""

    async def test_static_lists_carry_cache_hints(self, gateway):
        """``tools/list`` advertises the gateway's public, minutes-long freshness hint."""
        bundle = gateway._servers[0]
        async with Client(bundle.mcp) as mcp_client:
            result = await mcp_client.list_tools()
        dumped = result.model_dump(by_alias=True)
        assert dumped['ttlMs'] == 300_000
        assert dumped['cacheScope'] == 'public'

    def test_tool_registration_order_is_deterministic(self, petstore_json_path):
        """Two gateways built from the same spec register tools in the same order."""

        def tool_names() -> list[str]:
            config = GatewayConfig(servers=[ServerConfig(name='petstore', spec=str(petstore_json_path))])
            mcp = Gateway.from_config(config)._servers[0].mcp
            return [tool.name for tool in mcp._tool_manager.list_tools()]

        first = tool_names()
        assert first, 'petstore should register at least one tool'
        assert first == tool_names()
