import typing

import pytest

from openapi_mcp_gateway.auth.resolver import (
    NullAuthResolver,
    PassthroughAuthResolver,
    StaticAuthResolver,
)
from openapi_mcp_gateway.auth.types import AUTH_TYPE_HANDLERS, AuthTypeContext, build_auth
from openapi_mcp_gateway.openapi import OpenAPISpec
from openapi_mcp_gateway.settings import AuthConfig, ServerConfig
from openapi_mcp_gateway.stores.memory import MemoryTokenStore


def _context(auth: AuthConfig) -> AuthTypeContext:
    """Build the one shape every handler receives, for a server carrying ``auth``."""
    return AuthTypeContext(
        entry=ServerConfig(name='srv', spec='dummy.json', base_url='https://api.example.com', auth=auth),
        spec=OpenAPISpec(raw={}, title='test', version='1.0.0', operations=[], security_schemes={}),
        store=MemoryTokenStore(),
        gateway_url='https://gw.example.com',
    )


class TestStaticCredentials:
    """``bearer`` and ``api_key`` differ only in how the credential is framed."""

    def test_bearer_prefixes_the_token(self):
        """A bearer credential travels as ``Authorization: Bearer <token>``."""
        setup = build_auth(_context(AuthConfig(type='bearer', token='my-token')))

        assert isinstance(setup.resolver, StaticAuthResolver)
        assert setup.resolver._header_name == 'Authorization'
        assert setup.resolver._header_value == 'Bearer my-token'

    def test_api_key_sends_the_token_unprefixed(self):
        """An API key is the whole header value, on a header the API names."""
        setup = build_auth(_context(AuthConfig(type='api_key', token='key123')))

        assert isinstance(setup.resolver, StaticAuthResolver)
        assert setup.resolver._header_name == 'X-API-Key'
        assert setup.resolver._header_value == 'key123'

    def test_api_key_header_is_configurable(self):
        """``api_key_header`` names the header, since APIs disagree on it."""
        setup = build_auth(_context(AuthConfig(type='api_key', token='key123', api_key_header='X-Custom')))

        assert isinstance(setup.resolver, StaticAuthResolver)
        assert setup.resolver._header_name == 'X-Custom'

    def test_env_var_substitution_reaches_the_header(self, monkeypatch):
        """``${VAR}`` resolves on the way through, so config never holds the secret."""
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        setup = build_auth(_context(AuthConfig(type='bearer', token='${TEST_TOKEN}')))

        assert isinstance(setup.resolver, StaticAuthResolver)
        assert setup.resolver._header_value == 'Bearer env-token'

    def test_a_declared_type_without_a_token_sends_nothing(self):
        """A half-filled config serves the API unauthenticated rather than failing at startup.

        The upstream rejects the call, which is a clearer signal than a gateway that will not boot.
        """
        setup = build_auth(_context(AuthConfig(type='bearer')))

        assert isinstance(setup.resolver, NullAuthResolver)


class TestOtherTypes:
    """The remaining types need no credential of the gateway's own."""

    def test_none_sends_nothing(self):
        """A public API needs no header, and any stray token is ignored."""
        setup = build_auth(_context(AuthConfig(type='none', token='ignored')))

        assert isinstance(setup.resolver, NullAuthResolver)

    def test_passthrough_forwards_the_caller_header(self):
        """The caller's own credential travels on, which only the in-process case may do."""
        setup = build_auth(_context(AuthConfig(type='passthrough')))

        assert isinstance(setup.resolver, PassthroughAuthResolver)


class TestDispatch:
    """The registry is the extension point, so adding a type never edits a branch."""

    def test_every_declared_type_has_a_handler(self):
        """``AuthConfig.type`` and the registry must not drift apart.

        A value the config accepts but the registry does not know would fail only at startup,
        for the one deployment that happened to use it.
        """
        declared = set(typing.get_args(AuthConfig.model_fields['type'].annotation))

        assert declared == set(AUTH_TYPE_HANDLERS)

    def test_an_unknown_type_names_what_is_supported(self):
        """The error lists the alternatives rather than only rejecting the input."""
        context = _context(AuthConfig())
        context.entry.auth.type = 'invented'  # type: ignore[assignment]

        with pytest.raises(ValueError, match='unsupported auth type'):
            build_auth(context)
