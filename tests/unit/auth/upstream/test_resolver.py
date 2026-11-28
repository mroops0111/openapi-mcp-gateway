from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamCompositeAuthResolver,
    UpstreamNoCredentialAuthResolver,
    UpstreamPassthroughAuthResolver,
    UpstreamTokenSourceAuthResolver,
)
from tests.context import stub_context


@pytest.fixture
def mock_context():
    """Stand-in MCP context (most resolvers don't inspect it)."""
    return MagicMock()


class TestNoCredentialAuthResolver:
    """``UpstreamNoCredentialAuthResolver`` always resolves to an empty header dict."""

    async def test_returns_empty_dict(self, mock_context):
        """Every call returns ``{}`` regardless of context."""
        resolver = UpstreamNoCredentialAuthResolver()
        assert await resolver.resolve(mock_context) == {}


class TestTokenSourceAuthResolver:
    """``UpstreamTokenSourceAuthResolver`` delegates to a ``UpstreamTokenSource`` for dynamic bearer tokens."""

    async def test_returns_bearer_header(self, mock_context):
        """A token-source-supplied value is wrapped as ``Bearer <token>``."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='upstream-cc-token')
        resolver = UpstreamTokenSourceAuthResolver(token_source)
        assert await resolver.resolve(mock_context) == {'Authorization': 'Bearer upstream-cc-token'}

    async def test_returns_empty_dict_when_token_empty(self, mock_context):
        """An empty/falsy token from the source results in no headers."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='')
        resolver = UpstreamTokenSourceAuthResolver(token_source)
        assert await resolver.resolve(mock_context) == {}


class TestPassthroughAuthResolver:
    """``UpstreamPassthroughAuthResolver`` copies named headers from the live MCP request."""

    async def test_default_forwards_authorization_only(self):
        """By default the resolver only forwards the ``Authorization`` header."""
        ctx = stub_context({'authorization': 'Bearer client-token', 'x-api-key': 'key'})
        resolver = UpstreamPassthroughAuthResolver()
        assert await resolver.resolve(ctx) == {'Authorization': 'Bearer client-token'}

    async def test_forwards_configured_headers(self):
        """``header_names`` lets callers forward any incoming credential header."""
        ctx = stub_context({'authorization': 'Bearer client-token', 'x-api-key': 'key'})
        resolver = UpstreamPassthroughAuthResolver(header_names=('Authorization', 'X-API-Key'))
        assert await resolver.resolve(ctx) == {
            'Authorization': 'Bearer client-token',
            'X-API-Key': 'key',
        }

    async def test_skips_headers_absent_on_request(self):
        """Names not present on the request are simply omitted from the result."""
        ctx = stub_context({'authorization': 'Bearer t'})
        resolver = UpstreamPassthroughAuthResolver(header_names=('Authorization', 'X-API-Key'))
        assert await resolver.resolve(ctx) == {'Authorization': 'Bearer t'}

    async def test_returns_empty_dict_when_no_request_context(self):
        """A context without a live request (e.g. stdio) yields no headers."""
        ctx = MagicMock()
        ctx.request_context = None
        assert await UpstreamPassthroughAuthResolver().resolve(ctx) == {}

    async def test_returns_empty_dict_when_request_attr_missing(self):
        """A request_context without a ``.request`` attribute is treated as no request."""
        ctx = MagicMock()
        ctx.request_context = MagicMock(spec=[])
        assert await UpstreamPassthroughAuthResolver().resolve(ctx) == {}


class TestCompositeAuthResolver:
    """``UpstreamCompositeAuthResolver`` merges resolvers in order, so later entries override earlier ones."""

    async def test_merges_distinct_headers(self, mock_context):
        """Resolvers contributing distinct keys all show up in the result."""
        first = MagicMock()
        first.resolve = AsyncMock(return_value={'X-API-Key': 'k'})
        second = MagicMock()
        second.resolve = AsyncMock(return_value={'Authorization': 'Bearer cc'})
        composite = UpstreamCompositeAuthResolver([first, second])
        assert await composite.resolve(mock_context) == {
            'X-API-Key': 'k',
            'Authorization': 'Bearer cc',
        }

    async def test_later_resolver_overrides_earlier(self, mock_context):
        """When two resolvers set the same key, the later one wins."""
        first = MagicMock()
        first.resolve = AsyncMock(return_value={'Authorization': 'Bearer client'})
        second = MagicMock()
        second.resolve = AsyncMock(return_value={'Authorization': 'Bearer gateway'})
        composite = UpstreamCompositeAuthResolver([first, second])
        assert await composite.resolve(mock_context) == {'Authorization': 'Bearer gateway'}

    async def test_empty_composite_returns_empty(self, mock_context):
        """An empty resolver list yields an empty header dict (defensive default)."""
        assert await UpstreamCompositeAuthResolver([]).resolve(mock_context) == {}
