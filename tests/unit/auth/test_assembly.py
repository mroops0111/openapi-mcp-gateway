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

    def test_authorization_server_metadata_needs_the_settings_it_describes(self):
        """The metadata describes an authorization server the settings configure, so it cannot be served alone."""
        with pytest.raises(ValueError, match='needs the auth settings'):
            ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver(), authorization_server_metadata=MagicMock())

    def test_settings_need_the_protected_resource_document(self):
        """Each strategy names who mints its tokens, so the app never derives the document itself."""
        with pytest.raises(ValueError, match='needs the protected resource metadata'):
            ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver(), settings=MagicMock())
