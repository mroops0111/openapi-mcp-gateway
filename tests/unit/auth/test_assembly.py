from unittest.mock import MagicMock

import pytest

from openapi_mcp_gateway.auth.assembly import (
    AuthTypeContext,
    AuthTypeHandler,
    OAuthFlowContext,
    OAuthFlowHandler,
    ServerAuthSetup,
)
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
        """The protected resource document is built from the settings, so metadata alone cannot be served."""
        with pytest.raises(ValueError, match='needs the auth settings'):
            ServerAuthSetup(resolver=UpstreamNoCredentialAuthResolver(), authorization_server_metadata=MagicMock())


class TestUpstreamPrincipalDeclaration:
    """A handler added at the extension point without its principal is refused when its module loads."""

    def test_an_auth_type_handler_without_one_is_refused(self):
        with pytest.raises(TypeError, match='must declare upstream_principal'):

            class _Undeclared(AuthTypeHandler):
                def build(self, context: AuthTypeContext) -> ServerAuthSetup:
                    raise NotImplementedError

    def test_an_oauth_flow_handler_without_one_is_refused(self):
        with pytest.raises(TypeError, match='must declare upstream_principal'):

            class _Undeclared(OAuthFlowHandler):
                def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
                    raise NotImplementedError
