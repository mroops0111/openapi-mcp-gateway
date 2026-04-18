"""Tests for AuthResolver implementations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.resolver import (
    NullAuthResolver,
    OAuthAuthResolver,
    StaticAuthResolver,
)


@pytest.fixture
def mock_ctx():
    return MagicMock()


class TestNullAuthResolver:
    async def test_returns_none(self, mock_ctx):
        resolver = NullAuthResolver()
        result = await resolver.resolve(mock_ctx)
        assert result is None


class TestStaticAuthResolver:
    async def test_returns_header_value(self, mock_ctx):
        resolver = StaticAuthResolver('Bearer my-token')
        result = await resolver.resolve(mock_ctx)
        assert result == 'Bearer my-token'

    async def test_returns_api_key(self, mock_ctx):
        resolver = StaticAuthResolver('api-key-123')
        result = await resolver.resolve(mock_ctx)
        assert result == 'api-key-123'


class TestOAuthAuthResolver:
    async def test_returns_bearer_token(self, mock_ctx):
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value='upstream-token-xyz')
        resolver = OAuthAuthResolver(provider)
        result = await resolver.resolve(mock_ctx)
        assert result == 'Bearer upstream-token-xyz'

    async def test_returns_none_when_no_token(self, mock_ctx):
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value=None)
        resolver = OAuthAuthResolver(provider)
        result = await resolver.resolve(mock_ctx)
        assert result is None
