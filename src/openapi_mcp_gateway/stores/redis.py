import json
import logging
import typing

import redis.asyncio as aioredis

from .base import TokenStore


logger = logging.getLogger(__name__)


class RedisTokenStore(TokenStore):
    """Redis-backed ``TokenStore``.

    Uses native Redis TTL for automatic key expiry.
    All data is stored as JSON strings under a configurable key prefix.
    """

    def __init__(self, url: str = 'redis://localhost:6379', prefix: str = 'mcp_gw') -> None:
        self._prefix = prefix
        self._redis: aioredis.Redis = aioredis.from_url(url, decode_responses=True)
        logger.debug('RedisTokenStore connected: url=%s prefix=%s', url, prefix)

    def _key(self, namespace: str, key: str) -> str:
        return f'{self._prefix}:{namespace}:{key}'

    async def get(self, namespace: str, key: str) -> typing.Any | None:
        raw = await self._redis.get(self._key(namespace, key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, namespace: str, key: str, data: typing.Any, ttl: int | None = None) -> None:
        full_key = self._key(namespace, key)
        payload = json.dumps(data)
        if ttl is not None:
            await self._redis.setex(full_key, ttl, payload)
        else:
            await self._redis.set(full_key, payload)

    async def delete(self, namespace: str, key: str) -> None:
        await self._redis.delete(self._key(namespace, key))

    async def set_mapping(
        self,
        from_ns: str,
        from_key: str,
        to_ns: str,
        to_key: str,
        ttl: int | None = None,
    ) -> None:
        mapping_ns = f'{from_ns}__to__{to_ns}'
        await self.set(mapping_ns, from_key, to_key, ttl=ttl)

    async def get_mapping(self, from_ns: str, from_key: str, to_ns: str) -> typing.Any | None:
        mapping_ns = f'{from_ns}__to__{to_ns}'
        return await self.get(mapping_ns, from_key)

    async def close(self) -> None:
        await self._redis.aclose()
