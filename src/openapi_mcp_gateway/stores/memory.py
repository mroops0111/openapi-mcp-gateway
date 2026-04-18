"""In-memory token store — zero dependencies, suitable for dev and single-process deployments."""

import time
import typing

from .base import TokenStore


class MemoryTokenStore(TokenStore):
    """In-memory implementation of the TokenStore protocol.

    Data is lost on process restart. For production multi-process
    deployments, use RedisTokenStore instead.
    """

    def __init__(self) -> None:
        self._data: dict[str, typing.Any] = {}
        self._expiry: dict[str, float] = {}

    async def get(self, namespace: str, key: str) -> typing.Any | None:
        full_key = f'{namespace}:{key}'
        if full_key in self._expiry and time.time() > self._expiry[full_key]:
            del self._data[full_key]
            del self._expiry[full_key]
            return None
        return self._data.get(full_key)

    async def set(self, namespace: str, key: str, data: typing.Any, ttl: int | None = None) -> None:
        full_key = f'{namespace}:{key}'
        self._data[full_key] = data
        if ttl is not None:
            self._expiry[full_key] = time.time() + ttl
        elif full_key in self._expiry:
            del self._expiry[full_key]

    async def delete(self, namespace: str, key: str) -> None:
        full_key = f'{namespace}:{key}'
        self._data.pop(full_key, None)
        self._expiry.pop(full_key, None)

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
        self._data.clear()
        self._expiry.clear()
