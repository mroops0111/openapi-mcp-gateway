import pytest
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    DNSRebindingProtectionConfig,
    GatewayConfig,
    ServerConfig,
)
from tests.constants import (
    ATTACKER_HOST,
    ATTACKER_URL,
    BROWSER_ORIGIN,
    GATEWAY_HOST,
)


class TestDNSRebindingProtection:
    """``Host`` / ``Origin`` checks on the MCP endpoints, which is how a DNS rebinding attack is refused."""

    @pytest.fixture
    def listed_gateway(self, shop_json_path):
        """Shop gateway that lists the one host and origin it is reached by."""
        config = GatewayConfig(
            servers=[ServerConfig(name='shop', spec=str(shop_json_path))],
            dns_rebinding_protection=DNSRebindingProtectionConfig(
                allowed_hosts=[GATEWAY_HOST], allowed_origins=[BROWSER_ORIGIN]
            ),
        )
        return Gateway.from_config(config)

    def test_loopback_bind_refuses_a_foreign_host(self, gateway):
        """A page re-pointing its own domain at 127.0.0.1 still sends that domain as ``Host``, so it is refused."""
        with TestClient(gateway.build_app(host='127.0.0.1')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, 'localhost:8000') == 200

    def test_any_other_bind_accepts_any_host(self, gateway):
        """With nothing listed, a bind behind an unknown proxy keeps accepting whatever ``Host`` arrives."""
        with TestClient(gateway.build_app(host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 200

    def test_listed_hosts_and_origins_are_enforced_on_any_bind(self, listed_gateway):
        """Listing hosts turns the check on even for a ``0.0.0.0`` bind, and accepts only what is listed."""
        with TestClient(listed_gateway.build_app(host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, GATEWAY_HOST) == 200
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, GATEWAY_HOST, origin=BROWSER_ORIGIN) == 200
            assert _initialize_status(test_client, GATEWAY_HOST, origin=ATTACKER_URL) == 403


def _initialize_status(test_client: TestClient, host: str, origin: str | None = None) -> int:
    """Status of an MCP ``initialize`` POST to the shop endpoint, sent with ``host`` and ``origin``."""
    headers = {'Host': host, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    if origin is not None:
        headers['Origin'] = origin
    body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'initialize',
        'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}},
    }
    return test_client.post('/shop/mcp', headers=headers, json=body).status_code
