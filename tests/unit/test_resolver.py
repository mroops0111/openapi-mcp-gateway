from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.resolver import (
    AuthorizationCodeAuthResolver,
    NullAuthResolver,
    StaticAuthResolver,
    TokenSourceAuthResolver,
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


class TestAuthorizationCodeAuthResolver:
    """``AuthorizationCodeAuthResolver`` delegates to the provider and prepends ``Bearer ``."""

    async def test_returns_bearer_token(self, mock_context):
        """A provider-supplied token is returned as ``Bearer <token>``."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value='upstream-token-xyz')
        resolver = AuthorizationCodeAuthResolver(provider)
        result = await resolver.resolve(mock_context)
        assert result == 'Bearer upstream-token-xyz'

    async def test_returns_none_when_no_token(self, mock_context):
        """When the provider has no token, the resolver returns ``None`` (no header)."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value=None)
        resolver = AuthorizationCodeAuthResolver(provider)
        result = await resolver.resolve(mock_context)
        assert result is None


class TestTokenSourceAuthResolver:
    """``TokenSourceAuthResolver`` delegates to a ``TokenSource`` for dynamic bearer tokens."""

    async def test_returns_bearer_header(self, mock_context):
        """A token-source-supplied value is returned as ``Bearer <token>``."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='upstream-cc-token')
        resolver = TokenSourceAuthResolver(token_source)
        result = await resolver.resolve(mock_context)
        assert result == 'Bearer upstream-cc-token'

    async def test_returns_none_when_token_empty(self, mock_context):
        """An empty/falsy token from the source results in ``None`` (no header)."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='')
        resolver = TokenSourceAuthResolver(token_source)
        result = await resolver.resolve(mock_context)
        assert result is None
