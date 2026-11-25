import base64
import contextlib
import json
import pathlib
import typing

import httpx
import pytest
from mcp import Client

from tests.e2e.keycloak import CLIENT_SECRET, UPSTREAM_AUDIENCE, UPSTREAM_SCOPES, create_realm
from tests.external_issuer import (
    SCOPED_TOOLS,
    SHARED_CREDENTIAL_UPSTREAM,
    UPSTREAM_TOKEN,
    connect_as,
    external_issuer_gateway,
    issuer_declines_the_step_up,
    token_exchange_upstream,
)
from tests.serving import ServingAddress, serve_gateway
from tests.tool_result import text_of


pytestmark = pytest.mark.e2e


class TestTokenExchange:
    """Keycloak mints each agent's MCP token and exchanges it for one the upstream accepts."""

    async def test_a_read_exchanges_for_only_the_read_scope(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        async with _serve_token_exchange(secured_spec, gateway_address, issuer, 'reader') as client:
            names = sorted(tool.name for tool in (await client.list_tools()).tools)
            result = await client.call_tool('list_pets', {})

        assert names == SCOPED_TOOLS
        assert not result.is_error
        claims = _upstream_claims(upstream_requests[0])
        assert claims['aud'] == UPSTREAM_AUDIENCE
        assert claims['scope'].split() == ['read:pets']

    async def test_a_step_up_keycloak_grants_goes_through(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        async with _serve_token_exchange(secured_spec, gateway_address, issuer, 'writer') as client:
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert not result.is_error
        assert 'write:pets' in _upstream_claims(upstream_requests[0])['scope'].split()

    async def test_a_step_up_keycloak_refuses_never_reaches_the_upstream(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        with issuer_declines_the_step_up():
            async with _serve_token_exchange(secured_spec, gateway_address, issuer, 'reader') as client:
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert upstream_requests == []

    async def test_an_operation_needing_both_upstream_scopes_gets_both(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        async with _serve_token_exchange(secured_spec, gateway_address, issuer, 'writer') as client:
            result = await client.call_tool('delete_pet', {'petId': 1})

        assert not result.is_error
        assert set(_upstream_claims(upstream_requests[0])['scope'].split()) == {'read:pets', 'write:pets'}

    async def test_a_scope_keycloak_will_not_exchange_for_is_refused_before_the_upstream(
        self, keycloak, secured_spec, gateway_address, upstream_requests
    ):
        """No step-up can widen what the issuer exchanges, so the gateway refuses rather than call the upstream."""
        endpoint = f'{gateway_address.url}/petstore/mcp'
        with create_realm(keycloak, endpoint, exchangeable_scopes=['read:pets']) as issuer:
            async with _serve_token_exchange(secured_spec, gateway_address, issuer, 'writer') as client:
                result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert result.is_error
        assert 'write:pets' in text_of(result)
        assert upstream_requests == []


class TestSharedCredential:
    """Keycloak's tokens limit each caller on a server whose upstream sees only the gateway."""

    async def test_a_caller_reaches_only_what_keycloak_granted_it(
        self, petstore_json_path, gateway_address, issuer, upstream_requests
    ):
        with issuer_declines_the_step_up():
            async with (
                serve_gateway(
                    lambda url: external_issuer_gateway(petstore_json_path, url, issuer, SHARED_CREDENTIAL_UPSTREAM),
                    address=gateway_address,
                ) as base_url,
                connect_as(base_url, issuer, 'reader', CLIENT_SECRET) as client,
            ):
                await client.call_tool('list_pets', {})
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert [request.headers['authorization'] for request in upstream_requests] == [f'Bearer {UPSTREAM_TOKEN}']


@contextlib.asynccontextmanager
async def _serve_token_exchange(
    spec: pathlib.Path, address: ServingAddress, issuer: str, agent: str
) -> typing.AsyncIterator[Client]:
    """A petstore gateway exchanging each caller's Keycloak token, with ``agent`` signed in to it."""

    def build(url: str):
        upstream = token_exchange_upstream(UPSTREAM_AUDIENCE, f'{url}/petstore/mcp', CLIENT_SECRET, UPSTREAM_SCOPES)
        return external_issuer_gateway(spec, url, issuer, upstream)

    async with (
        serve_gateway(build, address=address) as base_url,
        connect_as(base_url, issuer, agent, CLIENT_SECRET) as client,
    ):
        yield client


def _upstream_claims(request: httpx.Request) -> dict[str, typing.Any]:
    """The claims of the token ``request`` carried upstream."""
    payload = request.headers['authorization'].removeprefix('Bearer ').split('.')[1]
    return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
