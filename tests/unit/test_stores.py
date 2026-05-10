import time
from unittest.mock import patch

import pytest


@pytest.fixture(params=['memory', 'redis'])
def store(request, memory_store, redis_store):
    """Parametrise tests across both store implementations."""
    if request.param == 'memory':
        return memory_store
    return redis_store


class TestStoreBasicCRUD:
    """Set/get/delete and namespace isolation across both store backends."""

    async def test_set_and_get(self, store):
        """A value set under a namespace+key round-trips through ``get``."""
        await store.set('ns', 'key1', {'foo': 'bar'})
        result = await store.get('ns', 'key1')
        assert result == {'foo': 'bar'}

    async def test_get_nonexistent(self, store):
        """``get`` on a missing key returns ``None``."""
        result = await store.get('ns', 'missing')
        assert result is None

    async def test_delete(self, store):
        """``delete`` removes a key so subsequent ``get`` returns ``None``."""
        await store.set('ns', 'key1', 'data')
        await store.delete('ns', 'key1')
        result = await store.get('ns', 'key1')
        assert result is None

    async def test_delete_nonexistent(self, store):
        """Deleting a missing key is a silent no-op."""
        await store.delete('ns', 'missing')

    async def test_overwrite(self, store):
        """Setting the same key twice keeps only the latest value."""
        await store.set('ns', 'key1', 'v1')
        await store.set('ns', 'key1', 'v2')
        result = await store.get('ns', 'key1')
        assert result == 'v2'

    async def test_namespace_isolation(self, store):
        """The same key in two namespaces does not collide."""
        await store.set('ns1', 'key', 'a')
        await store.set('ns2', 'key', 'b')
        assert await store.get('ns1', 'key') == 'a'
        assert await store.get('ns2', 'key') == 'b'

    async def test_complex_data(self, store):
        """Nested dict/list structures round-trip through serialisation."""
        data = {'token': 'abc', 'scopes': ['read', 'write'], 'nested': {'x': 1}}
        await store.set('ns', 'key', data)
        result = await store.get('ns', 'key')
        assert result == data


class TestStoreTTL:
    """Time-to-live semantics for both store backends."""

    async def test_ttl_not_expired(self, store):
        """A value within its TTL is still readable."""
        await store.set('ns', 'key', 'data', ttl=60)
        result = await store.get('ns', 'key')
        assert result == 'data'

    async def test_ttl_expired_memory(self, memory_store):
        """In-memory store evicts expired keys when ``time.time`` advances past TTL."""
        await memory_store.set('ns', 'key', 'data', ttl=1)
        with patch('time.time', return_value=time.time() + 2):
            result = await memory_store.get('ns', 'key')
        assert result is None

    async def test_ttl_expired_redis(self, redis_store):
        """Redis store sets the underlying key TTL (``-1`` when omitted)."""
        await redis_store.set('ns', 'key', 'data', ttl=60)
        full_key = redis_store._key('ns', 'key')
        ttl = await redis_store._redis.ttl(full_key)
        assert ttl > 0

        await redis_store.set('ns', 'key2', 'data')
        ttl_no_expiry = await redis_store._redis.ttl(redis_store._key('ns', 'key2'))
        assert ttl_no_expiry == -1

    async def test_set_without_ttl_removes_expiry(self, memory_store):
        """Re-setting a key without TTL clears any previous expiry record."""
        await memory_store.set('ns', 'key', 'data', ttl=10)
        await memory_store.set('ns', 'key', 'data2')
        full_key = 'ns:key'
        assert full_key not in memory_store._expiry


class TestStoreMapping:
    """Cross-namespace mappings used to link MCP tokens to upstream tokens."""

    async def test_set_and_get_mapping(self, store):
        """A mapping from one (ns, key) to another is retrievable."""
        await store.set_mapping('mcp_access', 'mcp_123', 'api_access', 'api_456', ttl=60)
        result = await store.get_mapping('mcp_access', 'mcp_123', 'api_access')
        assert result == 'api_456'

    async def test_mapping_nonexistent(self, store):
        """A missing mapping returns ``None``."""
        result = await store.get_mapping('mcp_access', 'missing', 'api_access')
        assert result is None

    async def test_multiple_mappings(self, store):
        """The same source can map to multiple target namespaces independently."""
        await store.set_mapping('mcp_access', 'mcp_1', 'api_access', 'api_a')
        await store.set_mapping('mcp_access', 'mcp_1', 'mcp_refresh', 'ref_b')
        assert await store.get_mapping('mcp_access', 'mcp_1', 'api_access') == 'api_a'
        assert await store.get_mapping('mcp_access', 'mcp_1', 'mcp_refresh') == 'ref_b'

    async def test_mapping_direction(self, store):
        """Mappings are directional, so the reverse lookup must not exist."""
        await store.set_mapping('from_ns', 'key', 'to_ns', 'value')
        assert await store.get_mapping('from_ns', 'key', 'to_ns') == 'value'
        assert await store.get_mapping('to_ns', 'value', 'from_ns') is None


class TestStoreClose:
    """``close`` semantics for both backends."""

    async def test_close_memory(self, memory_store):
        """Closing the memory store discards in-memory data."""
        await memory_store.set('ns', 'key', 'data')
        await memory_store.close()
        assert await memory_store.get('ns', 'key') is None

    async def test_close_redis(self, redis_store):
        """Closing the Redis store releases the underlying connection without error."""
        await redis_store.set('ns', 'key', 'data')
        await redis_store.close()
