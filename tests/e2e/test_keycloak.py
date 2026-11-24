import base64
import contextlib
import json
import pathlib
import typing

import httpx
import pytest
from mcp import Client
from mcp.client.auth.exceptions import OAuthTokenError

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.e2e.conftest import GatewayAddress
from tests.e2e.keycloak import CLIENT_SECRET, UPSTREAM_AUDIENCE, UPSTREAM_SCOPES
from tests.mcp_client import client_credentials_auth, connect
from tests.serving import serve


pytestmark = pytest.mark.e2e

# What every caller's token must carry, which the protected resource metadata tells a client to ask for first.
REQUIRED_SCOPES = ['pets:read']

# Every tool an operation scope covers on the petstore fixture.
SCOPED_TOOLS = ['create_pet', 'delete_pet', 'get_pet_by_id', 'list_pets']

# The one credential every call carries upstream on a shared-credential server.
UPSTREAM_TOKEN = 'upstream-pat'


def _token_exchange_gateway(spec: pathlib.Path, address: GatewayAddress, issuer: str) -> Gateway:
    """A petstore gateway exchanging each caller's Keycloak token, enforcing the upstream grant it returns."""
    upstream = {
        'type': 'oauth2',
        'flow': 'token_exchange',
        'audience': UPSTREAM_AUDIENCE,
        'client_id': f'{address.url}/petstore/mcp',
        'client_secret': CLIENT_SECRET,
        'scopes': UPSTREAM_SCOPES,
        'enforce_scopes': True,
    }
    return _gateway(spec, address, upstream, issuer)


def _shared_credential_gateway(spec: pathlib.Path, address: GatewayAddress, issuer: str) -> Gateway:
    """A petstore gateway reaching its upstream with one bearer token, accepting the tokens Keycloak mints."""
    return _gateway(spec, address, {'type': 'bearer', 'token': UPSTREAM_TOKEN}, issuer)


def _gateway(spec: pathlib.Path, address: GatewayAddress, upstream: dict, issuer: str) -> Gateway:
    mcp = {'issuer': issuer, 'required_scopes': REQUIRED_SCOPES, 'operation_scopes': {'auto': 'tags'}}
    server = {'name': 'petstore', 'spec': str(spec), 'auth': {'upstream': upstream, 'mcp': mcp}}
    return Gateway.from_config(GatewayConfig.model_validate({'url': address.url, 'servers': [server]}))


@contextlib.asynccontextmanager
async def _connect(gateway: Gateway, address: GatewayAddress, issuer: str, agent: str) -> typing.AsyncIterator[Client]:
    """The MCP SDK's own client of the served ``gateway``, signing in at Keycloak as ``agent``."""
    endpoint = f'{address.url}/petstore/mcp'
    auth = client_credentials_auth(endpoint, agent, CLIENT_SECRET, issuer=issuer)
    async with serve(gateway._build_app(transport='streamable-http'), address.sock), connect(endpoint, auth) as client:
        yield client


def _upstream_claims(request: httpx.Request) -> dict[str, typing.Any]:
    """The claims of the token ``request`` carried upstream."""
    payload = request.headers['authorization'].removeprefix('Bearer ').split('.')[1]
    return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))


def _issuer_declines_the_step_up() -> pytest.RaisesGroup[OAuthTokenError]:
    """What the SDK client raises once Keycloak refuses the scope a challenge named, out of its task group."""
    return pytest.RaisesGroup(pytest.RaisesExc(OAuthTokenError, match='invalid_scope'), flatten_subgroups=True)


class TestTokenExchange:
    """Keycloak mints each agent's MCP token and exchanges it for one the upstream accepts."""

    async def test_a_read_exchanges_for_only_the_read_scope(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        gateway = _token_exchange_gateway(secured_spec, gateway_address, issuer)
        async with _connect(gateway, gateway_address, issuer, 'reader') as client:
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
        gateway = _token_exchange_gateway(secured_spec, gateway_address, issuer)
        async with _connect(gateway, gateway_address, issuer, 'writer') as client:
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert not result.is_error
        assert 'write:pets' in _upstream_claims(upstream_requests[0])['scope'].split()

    async def test_a_step_up_keycloak_refuses_never_reaches_the_upstream(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        gateway = _token_exchange_gateway(secured_spec, gateway_address, issuer)
        with _issuer_declines_the_step_up():
            async with _connect(gateway, gateway_address, issuer, 'reader') as client:
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert upstream_requests == []

    async def test_an_operation_needing_both_upstream_scopes_gets_both(
        self, secured_spec, gateway_address, issuer, upstream_requests
    ):
        gateway = _token_exchange_gateway(secured_spec, gateway_address, issuer)
        async with _connect(gateway, gateway_address, issuer, 'writer') as client:
            result = await client.call_tool('delete_pet', {'petId': 1})

        assert not result.is_error
        assert set(_upstream_claims(upstream_requests[0])['scope'].split()) == {'read:pets', 'write:pets'}


class TestSharedCredential:
    """Keycloak's tokens limit each caller on a server whose upstream sees only the gateway."""

    async def test_a_caller_reaches_only_what_keycloak_granted_it(
        self, petstore_json_path, gateway_address, issuer, upstream_requests
    ):
        gateway = _shared_credential_gateway(petstore_json_path, gateway_address, issuer)
        with _issuer_declines_the_step_up():
            async with _connect(gateway, gateway_address, issuer, 'reader') as client:
                await client.call_tool('list_pets', {})
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert [request.headers['authorization'] for request in upstream_requests] == [f'Bearer {UPSTREAM_TOKEN}']
