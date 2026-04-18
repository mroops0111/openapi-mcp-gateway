"""Tests for MemoryTokenStore and RedisTokenStore (via fakeredis)."""

import time
from unittest.mock import patch

import pytest


@pytest.fixture(params=['memory', 'redis'])
def store(request, memory_store, redis_store):
    """Parametrize tests across both store implementations."""
    if request.param == 'memory':
        return memory_store
    return redis_store


class TestStoreBasicCRUD:
    async def test_set_and_get(self, store):
        await store.set('ns', 'key1', {'foo': 'bar'})
        result = await store.get('ns', 'key1')
        assert result == {'foo': 'bar'}

    async def test_get_nonexistent(self, store):
        result = await store.get('ns', 'missing')
        assert result is None

    async def test_delete(self, store):
        await store.set('ns', 'key1', 'data')
        await store.delete('ns', 'key1')
        result = await store.get('ns', 'key1')
        assert result is None

    async def test_delete_nonexistent(self, store):
        # Should not raise
        await store.delete('ns', 'missing')

    async def test_overwrite(self, store):
        await store.set('ns', 'key1', 'v1')
        await store.set('ns', 'key1', 'v2')
        result = await store.get('ns', 'key1')
        assert result == 'v2'

    async def test_namespace_isolation(self, store):
        await store.set('ns1', 'key', 'a')
        await store.set('ns2', 'key', 'b')
        assert await store.get('ns1', 'key') == 'a'
        assert await store.get('ns2', 'key') == 'b'

    async def test_complex_data(self, store):
        data = {'token': 'abc', 'scopes': ['read', 'write'], 'nested': {'x': 1}}
        await store.set('ns', 'key', data)
        result = await store.get('ns', 'key')
        assert result == data


class TestStoreTTL:
    async def test_ttl_not_expired(self, store):
        await store.set('ns', 'key', 'data', ttl=60)
        result = await store.get('ns', 'key')
        assert result == 'data'

    async def test_ttl_expired_memory(self, memory_store):
        """Memory store: simulate time passing."""
        await memory_store.set('ns', 'key', 'data', ttl=1)
        with patch('time.time', return_value=time.time() + 2):
            result = await memory_store.get('ns', 'key')
        assert result is None

    async def test_ttl_expired_redis(self, redis_store):
        """Redis store: verify key was set with TTL."""
        await redis_store.set('ns', 'key', 'data', ttl=60)
        full_key = redis_store._key('ns', 'key')
        ttl = await redis_store._redis.ttl(full_key)
        assert ttl > 0

        # Key without TTL should return -1 (no expiry)
        await redis_store.set('ns', 'key2', 'data')
        ttl2 = await redis_store._redis.ttl(redis_store._key('ns', 'key2'))
        assert ttl2 == -1

    async def test_set_without_ttl_removes_expiry(self, memory_store):
        await memory_store.set('ns', 'key', 'data', ttl=10)
        await memory_store.set('ns', 'key', 'data2')
        full_key = 'ns:key'
        assert full_key not in memory_store._expiry


class TestStoreMapping:
    async def test_set_and_get_mapping(self, store):
        await store.set_mapping('mcp_access', 'mcp_123', 'api_access', 'api_456', ttl=60)
        result = await store.get_mapping('mcp_access', 'mcp_123', 'api_access')
        assert result == 'api_456'

    async def test_mapping_nonexistent(self, store):
        result = await store.get_mapping('mcp_access', 'missing', 'api_access')
        assert result is None

    async def test_multiple_mappings(self, store):
        await store.set_mapping('mcp_access', 'mcp_1', 'api_access', 'api_a')
        await store.set_mapping('mcp_access', 'mcp_1', 'mcp_refresh', 'ref_b')
        assert await store.get_mapping('mcp_access', 'mcp_1', 'api_access') == 'api_a'
        assert await store.get_mapping('mcp_access', 'mcp_1', 'mcp_refresh') == 'ref_b'

    async def test_mapping_direction(self, store):
        """Mappings are directional — reverse should not exist."""
        await store.set_mapping('from_ns', 'key', 'to_ns', 'value')
        assert await store.get_mapping('from_ns', 'key', 'to_ns') == 'value'
        assert await store.get_mapping('to_ns', 'value', 'from_ns') is None


class TestStoreClose:
    async def test_close_memory(self, memory_store):
        await memory_store.set('ns', 'key', 'data')
        await memory_store.close()
        assert await memory_store.get('ns', 'key') is None

    async def test_close_redis(self, redis_store):
        await redis_store.set('ns', 'key', 'data')
        await redis_store.close()
        # After close, redis connection is closed
