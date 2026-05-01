"""Shared fixtures for the test suite."""

import json
import pathlib

import fakeredis.aioredis
import pytest
import pytest_asyncio

from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from openapi_mcp_gateway.stores.redis import RedisTokenStore


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
