from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.resolver import (
    NullAuthResolver,
    OAuthAuthResolver,
    StaticAuthResolver,
)


@pytest.fixture
def mock_context():
    """Stand-in MCP context — resolvers don't currently inspect it."""
    return MagicMock()


class TestNullAuthResolver:
    """``NullAuthResolver`` always resolves to ``None``."""

    async def test_returns_none(self, mock_context):
        """Every call returns ``None`` regardless of context."""
        resolver = NullAuthResolver()
        result = await resolver.resolve(mock_context)
        assert result is None


class TestStaticAuthResolver:
    """``StaticAuthResolver`` returns its configured header value verbatim."""

    async def test_returns_bearer_header(self, mock_context):
        """A ``Bearer …`` value is returned as-is."""
        resolver = StaticAuthResolver('Bearer my-token')
        result = await resolver.resolve(mock_context)
        assert result == 'Bearer my-token'

    async def test_returns_api_key(self, mock_context):
        """A bare api-key value is returned as-is (no scheme prefix added)."""
        resolver = StaticAuthResolver('api-key-123')
        result = await resolver.resolve(mock_context)
        assert result == 'api-key-123'


class TestOAuthAuthResolver:
    """``OAuthAuthResolver`` delegates to the provider and prepends ``Bearer ``."""

    async def test_returns_bearer_token(self, mock_context):
        """A provider-supplied token is returned as ``Bearer <token>``."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value='upstream-token-xyz')
        resolver = OAuthAuthResolver(provider)
        result = await resolver.resolve(mock_context)
        assert result == 'Bearer upstream-token-xyz'

    async def test_returns_none_when_no_token(self, mock_context):
        """When the provider has no token, the resolver returns ``None`` (no header)."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value=None)
        resolver = OAuthAuthResolver(provider)
        result = await resolver.resolve(mock_context)
        assert result is None
