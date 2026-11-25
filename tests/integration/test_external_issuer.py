import contextlib
import pathlib
import typing

import pytest
from mcp import Client

from tests.constants import API_URL
from tests.external_issuer import (
    SCOPED_TOOLS,
    SHARED_CREDENTIAL_UPSTREAM,
    UPSTREAM_TOKEN,
    connect_as,
    external_issuer_gateway,
    issuer_declines_the_step_up,
    token_exchange_upstream,
)
from tests.fake_idp import GATEWAY_CLIENT_ID, GATEWAY_CLIENT_SECRET, FakeIdentityProvider, serve_identity_provider
from tests.serving import serve_gateway
from tests.tool_result import text_of


# The agents the issuer mints tokens to, each with its own secret, by what each may be granted.
READER = 'reader'
WRITER = 'writer'
SECRETS = {READER: 'reader-secret', WRITER: 'writer-secret', GATEWAY_CLIENT_ID: GATEWAY_CLIENT_SECRET}
GRANTABLE = {READER: {'pets:read'}, WRITER: {'pets:read', 'pets:write'}}

# The most the gateway asks the issuer for upstream.
UPSTREAM_CEILING = ['read:pets', 'write:pets']


@pytest.fixture
def identity_provider(signing_key) -> typing.Iterator[FakeIdentityProvider]:
    """The organisation's issuer, served for the length of a test."""
    provider = FakeIdentityProvider(signing_key, SECRETS, {client: set(scopes) for client, scopes in GRANTABLE.items()})
    with serve_identity_provider(provider):
        yield provider


class TestTokenExchangeScopes:
    """Under ``token_exchange`` the issuer decides each token's MCP scopes, and the gateway only checks them."""

    async def test_every_scoped_tool_is_listed_for_a_step_up(self, identity_provider, secured_spec, upstream_requests):
        """The gateway cannot know what the issuer would grant, so a tool hidden now could never be stepped up for."""
        async with _serve_token_exchange(secured_spec, identity_provider, READER, 'pets:read') as client:
            names = sorted(tool.name for tool in (await client.list_tools()).tools)

        assert names == SCOPED_TOOLS

    async def test_a_step_up_the_issuer_grants_goes_through(self, identity_provider, secured_spec, upstream_requests):
        """The client asks the issuer for the scope the challenge names, and the call goes on with the new token."""
        async with _serve_token_exchange(secured_spec, identity_provider, WRITER, 'pets:read') as client:
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert not result.is_error
        assert [request.method for request in upstream_requests] == ['POST']
        assert upstream_requests[0].headers['authorization'].startswith('Bearer upstream-token-')

    async def test_a_step_up_the_issuer_refuses_never_reaches_the_upstream(
        self, identity_provider, secured_spec, upstream_requests
    ):
        """The SDK client gives up once the issuer declines, so the call never goes out."""
        with issuer_declines_the_step_up():
            async with _serve_token_exchange(secured_spec, identity_provider, READER, 'pets:read') as client:
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert upstream_requests == []

    async def test_each_exchange_asks_for_what_the_callers_scopes_open(
        self, identity_provider, secured_spec, upstream_requests
    ):
        """A caller that may only read pets is exchanged a token that may only read them upstream."""
        async with _serve_token_exchange(secured_spec, identity_provider, READER, 'pets:read') as client:
            await client.call_tool('list_pets', {})

        assert [exchange['scope'] for exchange in identity_provider.exchanges] == ['read:pets']

    async def test_a_short_upstream_grant_is_a_tool_error(self, identity_provider, secured_spec, upstream_requests):
        """No step-up can widen what the issuer exchanges, so the gateway refuses before calling the upstream."""
        identity_provider.upstream_grantable = {'read:pets'}
        async with _serve_token_exchange(secured_spec, identity_provider, WRITER, 'pets:read pets:write') as client:
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert result.is_error
        assert 'write:pets' in text_of(result)
        assert upstream_requests == []


class TestSharedCredentialScopes:
    """A server whose upstream sees only the gateway still limits each caller to what the issuer granted it."""

    async def test_a_caller_reaches_only_what_its_scopes_open(
        self, identity_provider, petstore_json_path, upstream_requests
    ):
        issuer = identity_provider.issuer
        with issuer_declines_the_step_up():
            async with (
                serve_gateway(
                    lambda url: external_issuer_gateway(petstore_json_path, url, issuer, SHARED_CREDENTIAL_UPSTREAM)
                ) as base_url,
                connect_as(base_url, issuer, READER, SECRETS[READER], scope='pets:read') as client,
            ):
                await client.call_tool('list_pets', {})
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert [request.headers['authorization'] for request in upstream_requests] == [f'Bearer {UPSTREAM_TOKEN}']
        assert identity_provider.exchanges == []


@contextlib.asynccontextmanager
async def _serve_token_exchange(
    spec: pathlib.Path, identity_provider: FakeIdentityProvider, agent: str, scope: str
) -> typing.AsyncIterator[Client]:
    """A petstore gateway exchanging each caller's token at the issuer, with ``agent`` signed in for ``scope``."""
    issuer = identity_provider.issuer
    upstream = token_exchange_upstream(API_URL, GATEWAY_CLIENT_ID, GATEWAY_CLIENT_SECRET, UPSTREAM_CEILING)
    async with (
        serve_gateway(lambda url: external_issuer_gateway(spec, url, issuer, upstream)) as base_url,
        connect_as(base_url, issuer, agent, SECRETS[agent], scope=scope) as client,
    ):
        yield client
