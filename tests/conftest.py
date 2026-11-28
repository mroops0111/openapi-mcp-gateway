import json
import pathlib
import typing

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa

from openapi_mcp_gateway.exposure import UpstreamHttpClient
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from openapi_mcp_gateway.stores.redis import RedisTokenStore
from tests.spec import write_secured_shop


# Where the spec fixtures every test reads live.
FIXTURES_DIR = pathlib.Path(__file__).parent / 'fixtures'


# What answers an upstream request in place of the network.
_HttpHandler = typing.Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def shop_json_path() -> pathlib.Path:
    return FIXTURES_DIR / 'shop.json'


@pytest.fixture
def shop_yml_path() -> pathlib.Path:
    return FIXTURES_DIR / 'shop.yml'


@pytest.fixture
def secured_spec(shop_json_path: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    """The shop fixture with each operation's upstream scopes declared."""
    return write_secured_shop(shop_json_path, tmp_path)


@pytest.fixture
def client_credentials_spec_path() -> pathlib.Path:
    """A spec declaring only the ``clientCredentials`` flow, so the flow is resolved rather than configured."""
    return FIXTURES_DIR / 'client_credentials.json'


@pytest.fixture
def shop_spec_raw() -> dict:
    return json.loads((FIXTURES_DIR / 'shop.json').read_text())


@pytest.fixture(scope='session')
def signing_key() -> rsa.RSAPrivateKey:
    """One RSA keypair an external issuer signs with, shared by the session since generation dominates the runtime."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest_asyncio.fixture
async def memory_store():
    store = MemoryTokenStore()
    yield store
    await store.close()


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest_asyncio.fixture
async def redis_store(fake_redis):
    store = RedisTokenStore(prefix='test', client=fake_redis)
    yield store
    await store.close()


@pytest.fixture
def mock_upstream(monkeypatch):
    """Route every ``UpstreamHttpClient`` upstream request through ``httpx.MockTransport``.

    Returns a one-shot installer ``mock_upstream(handler)``.
    Subsequent ``UpstreamHttpClient(...)`` constructions in this test will dispatch HTTP traffic to ``handler``,
    instead of the real network.
    """

    def install(handler: _HttpHandler) -> None:
        original_init = UpstreamHttpClient.__init__

        def patched_init(self, base_url, headers=None, timeout=90, transport=None):
            del transport  # MockTransport always wins so an in-process ASGI is overridden in tests.
            # Only the transport is swapped, so the client keeps the cookie policy and limits it has in production.
            original_init(self, base_url, headers=headers, timeout=timeout, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(UpstreamHttpClient, '__init__', patched_init)

    return install


@pytest.fixture
def upstream_requests(mock_upstream) -> list[httpx.Request]:
    """Answer every upstream request with an empty 200 and return the list they are recorded in, in call order."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    mock_upstream(record)
    return requests


@pytest.fixture
def constructed_upstream_clients(monkeypatch) -> list[UpstreamHttpClient]:
    """Every ``UpstreamHttpClient`` built from here on, in order, each still built as it would be without this."""
    constructed: list[UpstreamHttpClient] = []
    original_init = UpstreamHttpClient.__init__

    def recording_init(self, *args, **kwargs):
        constructed.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(UpstreamHttpClient, '__init__', recording_init)
    return constructed
