from openapi_mcp_gateway.app import _transport_security
from openapi_mcp_gateway.settings import DNSRebindingProtectionConfig
from tests.constants import GATEWAY_HOST


class TestTransportSecurity:
    """The ``Host`` and ``Origin`` checks the MCP endpoints run, settled from the config and the bind."""

    def test_mounted_gateway_checks_only_when_hosts_are_listed(self):
        """A mounted gateway has no bind of its own to judge by, so only a listed host turns the check on."""
        unlisted = _transport_security(DNSRebindingProtectionConfig(), host=None)
        assert unlisted is not None
        assert not unlisted.enable_dns_rebinding_protection
        listed = _transport_security(DNSRebindingProtectionConfig(allowed_hosts=[GATEWAY_HOST]), host=None)
        assert listed is not None
        assert listed.enable_dns_rebinding_protection
        assert listed.allowed_hosts == [GATEWAY_HOST]
