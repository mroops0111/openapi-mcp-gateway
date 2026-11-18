from unittest.mock import MagicMock

import pytest

from openapi_mcp_gateway.auth.assembly import ServerAuthSetup
from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamNoCredentialAuthResolver,
)


class TestServerAuthSetup:
    """The setup every handler returns refuses a shape the MCP SDK cannot serve."""

    def test_a_provider_and_a_verifier_are_exclusive(self):
        """Issuing tokens and validating someone else's are alternatives rather than layers."""
        with pytest.raises(ValueError, match='not both'):
            ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver(), provider=MagicMock(), verifier=MagicMock())
