import json
import pathlib
import typing

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio

from openapi_mcp_gateway.client import APIClient
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from openapi_mcp_gateway.stores.redis import RedisTokenStore


_HttpHandler = typing.Callable[[httpx.Request], httpx.Response]


FIXTURES_DIR = pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture
def petstore_json_path() -> pathlib.Path:
    return FIXTURES_DIR / 'petstore.json'


@pytest.fixture
def petstore_yml_path() -> pathlib.Path:
    return FIXTURES_DIR / 'petstore.yml'


@pytest.fixture
def petstore_spec_raw() -> dict:
    return json.loads((FIXTURES_DIR / 'petstore.json').read_text())


@pytest_asyncio.fixture
async def memory_store():
    store = MemoryTokenStore()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def redis_store():
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTokenStore.__new__(RedisTokenStore)
    store._prefix = 'test'
    store._redis = fake_redis
    yield store
    await store.close()


@pytest.fixture
def mock_upstream(monkeypatch):
    """Route every ``APIClient`` upstream request through ``httpx.MockTransport``.

    Returns a one-shot installer ``mock_upstream(handler)``; subsequent
    ``APIClient(...)`` constructions in this test will dispatch HTTP traffic to
    ``handler`` instead of the real network.
    """
    def install(handler: _HttpHandler) -> None:
        def patched_init(self, base_url, headers=None, timeout=90):
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers=headers or {},
                transport=httpx.MockTransport(handler),
            )
        monkeypatch.setattr(APIClient, '__init__', patched_init)
    return install
