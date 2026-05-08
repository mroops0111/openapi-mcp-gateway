from unittest.mock import AsyncMock, MagicMock

import pytest

from openapi_mcp_gateway.auth.resolver import (
    AuthorizationCodeAuthResolver,
    CompositeAuthResolver,
    NullAuthResolver,
    PassthroughAuthResolver,
    StaticAuthResolver,
    TokenSourceAuthResolver,
)


@pytest.fixture
def mock_context():
    """Stand-in MCP context — most resolvers don't inspect it."""
    return MagicMock()


class TestNullAuthResolver:
    """``NullAuthResolver`` always resolves to an empty header dict."""

    async def test_returns_empty_dict(self, mock_context):
        """Every call returns ``{}`` regardless of context."""
        resolver = NullAuthResolver()
        assert await resolver.resolve(mock_context) == {}


class TestStaticAuthResolver:
    """``StaticAuthResolver`` returns one fixed header — name + value chosen at construction."""

    async def test_default_header_name_is_authorization(self, mock_context):
        """Without an override the resolver targets the ``Authorization`` header."""
        resolver = StaticAuthResolver('Bearer my-token')
        assert await resolver.resolve(mock_context) == {'Authorization': 'Bearer my-token'}

    async def test_custom_header_name(self, mock_context):
        """``header_name`` lets api-key style schemes route to a non-Authorization field."""
        resolver = StaticAuthResolver('api-key-123', header_name='X-API-Key')
        assert await resolver.resolve(mock_context) == {'X-API-Key': 'api-key-123'}


class TestAuthorizationCodeAuthResolver:
    """``AuthorizationCodeAuthResolver`` delegates to the provider and emits ``Authorization``."""

    async def test_returns_bearer_when_provider_has_token(self, mock_context):
        """A provider-supplied token is wrapped as ``Bearer <token>`` under ``Authorization``."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value='upstream-token-xyz')
        resolver = AuthorizationCodeAuthResolver(provider)
        assert await resolver.resolve(mock_context) == {'Authorization': 'Bearer upstream-token-xyz'}

    async def test_returns_empty_dict_when_no_token(self, mock_context):
        """When the provider has no token, the resolver contributes no headers."""
        provider = MagicMock()
        provider.get_api_access_token = AsyncMock(return_value=None)
        resolver = AuthorizationCodeAuthResolver(provider)
        assert await resolver.resolve(mock_context) == {}


class TestTokenSourceAuthResolver:
    """``TokenSourceAuthResolver`` delegates to a ``TokenSource`` for dynamic bearer tokens."""

    async def test_returns_bearer_header(self, mock_context):
        """A token-source-supplied value is wrapped as ``Bearer <token>``."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='upstream-cc-token')
        resolver = TokenSourceAuthResolver(token_source)
        assert await resolver.resolve(mock_context) == {'Authorization': 'Bearer upstream-cc-token'}

    async def test_returns_empty_dict_when_token_empty(self, mock_context):
        """An empty/falsy token from the source results in no headers."""
        token_source = MagicMock()
        token_source.get_token = AsyncMock(return_value='')
        resolver = TokenSourceAuthResolver(token_source)
        assert await resolver.resolve(mock_context) == {}


def _ctx_with_headers(headers: dict[str, str] | None) -> MagicMock:
    """Stand-in MCP context whose request carries the given header map."""
    request = MagicMock()
    request.headers = headers if headers is not None else {}
    ctx = MagicMock()
    ctx.request_context.request = request
    return ctx


class TestPassthroughAuthResolver:
    """``PassthroughAuthResolver`` copies named headers from the live MCP request."""

    async def test_default_forwards_authorization_only(self):
        """By default the resolver only forwards the ``Authorization`` header."""
        ctx = _ctx_with_headers({'authorization': 'Bearer client-token', 'x-api-key': 'key'})
        resolver = PassthroughAuthResolver()
        assert await resolver.resolve(ctx) == {'Authorization': 'Bearer client-token'}

    async def test_forwards_configured_headers(self):
        """``header_names`` lets callers forward any incoming credential header."""
        ctx = _ctx_with_headers({'authorization': 'Bearer client-token', 'x-api-key': 'key'})
        resolver = PassthroughAuthResolver(header_names=('Authorization', 'X-API-Key'))
        assert await resolver.resolve(ctx) == {
            'Authorization': 'Bearer client-token',
            'X-API-Key': 'key',
        }

    async def test_skips_headers_absent_on_request(self):
        """Names not present on the request are simply omitted from the result."""
        ctx = _ctx_with_headers({'authorization': 'Bearer t'})
        resolver = PassthroughAuthResolver(header_names=('Authorization', 'X-API-Key'))
        assert await resolver.resolve(ctx) == {'Authorization': 'Bearer t'}

    async def test_returns_empty_dict_when_no_request_context(self):
        """A context without a live request (e.g. stdio) yields no headers."""
        ctx = MagicMock()
        ctx.request_context = None
        assert await PassthroughAuthResolver().resolve(ctx) == {}

    async def test_returns_empty_dict_when_request_attr_missing(self):
        """A request_context without a ``.request`` attribute is treated as no request."""
        ctx = MagicMock()
        ctx.request_context = MagicMock(spec=[])
        assert await PassthroughAuthResolver().resolve(ctx) == {}


class TestCompositeAuthResolver:
    """``CompositeAuthResolver`` merges resolvers in order; later entries override earlier ones."""

    async def test_merges_distinct_headers(self, mock_context):
        """Resolvers contributing distinct keys all show up in the result."""
        first = MagicMock()
        first.resolve = AsyncMock(return_value={'X-API-Key': 'k'})
        second = MagicMock()
        second.resolve = AsyncMock(return_value={'Authorization': 'Bearer cc'})
        composite = CompositeAuthResolver([first, second])
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
        composite = CompositeAuthResolver([first, second])
        assert await composite.resolve(mock_context) == {'Authorization': 'Bearer gateway'}

    async def test_empty_composite_returns_empty(self, mock_context):
        """An empty resolver list yields an empty header dict (defensive default)."""
        assert await CompositeAuthResolver([]).resolve(mock_context) == {}
