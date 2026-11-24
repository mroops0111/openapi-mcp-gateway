import contextlib
import pathlib
import typing

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.auth.exceptions import OAuthTokenError

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import API_URL
from tests.fake_idp import GATEWAY_CLIENT_ID, GATEWAY_CLIENT_SECRET, FakeIdentityProvider, serve_identity_provider
from tests.mcp_client import client_credentials_auth, connect
from tests.serving import serve_gateway


# The agents the issuer mints tokens to, each with its own secret, by what each may be granted.
READER = 'reader'
WRITER = 'writer'
SECRETS = {READER: 'reader-secret', WRITER: 'writer-secret', GATEWAY_CLIENT_ID: GATEWAY_CLIENT_SECRET}
GRANTABLE = {READER: {'pets:read'}, WRITER: {'pets:read', 'pets:write'}}

# The most the gateway asks the issuer for upstream.
UPSTREAM_CEILING = ['read:pets', 'write:pets']

# The one credential every call carries upstream on a shared-credential server.
UPSTREAM_TOKEN = 'upstream-pat'

# Every tool an operation scope covers on the petstore fixture, which the operations without a tag are not.
SCOPED_TOOLS = ['list_pets', 'create_pet', 'get_pet_by_id', 'delete_pet']


@pytest.fixture(scope='module')
def signing_key() -> rsa.RSAPrivateKey:
    """One RSA keypair for the module, since generation dominates the runtime."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def identity_provider(signing_key) -> typing.Iterator[FakeIdentityProvider]:
    """The organisation's issuer, served for the length of a test."""
    provider = FakeIdentityProvider(signing_key, SECRETS, {client: set(scopes) for client, scopes in GRANTABLE.items()})
    with serve_identity_provider(provider):
        yield provider


def _token_exchange_gateway(spec: pathlib.Path, issuer: str, **upstream: typing.Any) -> typing.Callable[[str], Gateway]:
    """A petstore gateway exchanging each caller's token at ``issuer``, enforcing the upstream grant it returns."""

    def build(url: str) -> Gateway:
        auth = {
            'upstream': {
                'type': 'oauth2',
                'flow': 'token_exchange',
                'audience': API_URL,
                'client_id': GATEWAY_CLIENT_ID,
                'client_secret': GATEWAY_CLIENT_SECRET,
                'scopes': UPSTREAM_CEILING,
                'enforce_scopes': True,
                **upstream,
            },
            'mcp': {'issuer': issuer, 'operation_scopes': {'auto': 'tags'}},
        }
        server = {'name': 'petstore', 'spec': str(spec), 'auth': auth}
        return Gateway.from_config(GatewayConfig.model_validate({'url': url, 'servers': [server]}))

    return build


def _shared_credential_gateway(spec: pathlib.Path, issuer: str) -> typing.Callable[[str], Gateway]:
    """A petstore gateway reaching its upstream with one bearer token, accepting the tokens ``issuer`` mints."""

    def build(url: str) -> Gateway:
        auth = {
            'upstream': {'type': 'bearer', 'token': UPSTREAM_TOKEN},
            'mcp': {'issuer': issuer, 'operation_scopes': {'auto': 'tags'}},
        }
        server = {'name': 'petstore', 'spec': str(spec), 'auth': auth}
        return Gateway.from_config(GatewayConfig.model_validate({'url': url, 'servers': [server]}))

    return build


@contextlib.asynccontextmanager
async def _connect(base_url: str, issuer: str, agent: str, scope: str) -> typing.AsyncIterator[Client]:
    """The MCP SDK's own client of the petstore, holding a token ``issuer`` minted to ``agent`` for ``scope``."""
    endpoint = f'{base_url}/petstore/mcp'
    auth = client_credentials_auth(endpoint, agent, SECRETS[agent], scope=scope, issuer=issuer)
    async with connect(endpoint, auth) as client:
        yield client


def _issuer_declines_the_step_up() -> pytest.RaisesGroup[OAuthTokenError]:
    """What the SDK client raises once the issuer refuses the scope a challenge named, out of its task group."""
    return pytest.RaisesGroup(pytest.RaisesExc(OAuthTokenError, match='invalid_scope'), flatten_subgroups=True)


class TestTokenExchangeScopes:
    """Under ``token_exchange`` the issuer decides each token's MCP scopes, and the gateway only checks them."""

    async def test_every_scoped_tool_is_listed_for_a_step_up(self, identity_provider, secured_spec, upstream_requests):
        """The gateway cannot know what the issuer would grant, so a tool hidden now could never be stepped up for."""
        async with (
            serve_gateway(_token_exchange_gateway(secured_spec, identity_provider.issuer)) as base_url,
            _connect(base_url, identity_provider.issuer, READER, 'pets:read') as client,
        ):
            names = [tool.name for tool in (await client.list_tools()).tools]

        assert sorted(names) == sorted(SCOPED_TOOLS)

    async def test_a_step_up_the_issuer_grants_goes_through(self, identity_provider, secured_spec, upstream_requests):
        """The client asks the issuer for the scope the challenge names, and the call goes on with the new token."""
        async with (
            serve_gateway(_token_exchange_gateway(secured_spec, identity_provider.issuer)) as base_url,
            _connect(base_url, identity_provider.issuer, WRITER, 'pets:read') as client,
        ):
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert not result.is_error
        assert [request.method for request in upstream_requests] == ['POST']
        assert upstream_requests[0].headers['authorization'].startswith('Bearer upstream-token-')

    async def test_a_step_up_the_issuer_refuses_never_reaches_the_upstream(
        self, identity_provider, secured_spec, upstream_requests
    ):
        """The SDK client gives up once the issuer declines, so the call never goes out."""
        with _issuer_declines_the_step_up():
            async with (
                serve_gateway(_token_exchange_gateway(secured_spec, identity_provider.issuer)) as base_url,
                _connect(base_url, identity_provider.issuer, READER, 'pets:read') as client,
            ):
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert upstream_requests == []

    async def test_each_exchange_asks_for_what_the_callers_scopes_open(
        self, identity_provider, secured_spec, upstream_requests
    ):
        """A caller that may only read pets is exchanged a token that may only read them upstream."""
        async with (
            serve_gateway(_token_exchange_gateway(secured_spec, identity_provider.issuer)) as base_url,
            _connect(base_url, identity_provider.issuer, READER, 'pets:read') as client,
        ):
            await client.call_tool('list_pets', {})

        assert [exchange['scope'] for exchange in identity_provider.exchanges] == ['read:pets']

    async def test_a_short_upstream_grant_is_a_tool_error(self, identity_provider, secured_spec, upstream_requests):
        """No step-up can widen what the issuer exchanges, so the gateway refuses before calling the upstream."""
        identity_provider.upstream_grantable = {'read:pets'}
        async with (
            serve_gateway(_token_exchange_gateway(secured_spec, identity_provider.issuer)) as base_url,
            _connect(base_url, identity_provider.issuer, WRITER, 'pets:read pets:write') as client,
        ):
            result = await client.call_tool('create_pet', {'name': 'Rex'})

        assert result.is_error
        assert 'write:pets' in str(result.content)
        assert upstream_requests == []


class TestSharedCredentialScopes:
    """A server whose upstream sees only the gateway still limits each caller to what the issuer granted it."""

    async def test_a_caller_reaches_only_what_its_scopes_open(
        self, identity_provider, petstore_json_path, upstream_requests
    ):
        with _issuer_declines_the_step_up():
            async with (
                serve_gateway(_shared_credential_gateway(petstore_json_path, identity_provider.issuer)) as base_url,
                _connect(base_url, identity_provider.issuer, READER, 'pets:read') as client,
            ):
                await client.call_tool('list_pets', {})
                await client.call_tool('create_pet', {'name': 'Rex'})

        assert [request.headers['authorization'] for request in upstream_requests] == [f'Bearer {UPSTREAM_TOKEN}']
        assert identity_provider.exchanges == []
