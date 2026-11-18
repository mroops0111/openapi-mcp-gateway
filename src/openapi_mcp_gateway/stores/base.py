import abc
import typing


class TokenStore(abc.ABC):
    """Namespaced key-value persistence with TTL and directional namespace mappings.

    Used to correlate MCP OAuth artefacts with upstream API credentials.
    The gateway keeps its records in these namespaces:

    - **MCP Side**: ``mcp_client``, ``mcp_access_token``, ``mcp_refresh_token``, ``mcp_auth_request``,
      ``mcp_auth_code`` and ``mcp_consent``
    - **Upstream Side**: ``upstream_auth_state``

    A mapping's target namespace may also be a link name with no records of its own,
    such as ``upstream_access_token`` for the upstream token an MCP token links.
    """

    @abc.abstractmethod
    async def get(self, namespace: str, key: str) -> typing.Any | None:
        """Look up data by ``(namespace, key)``."""

    @abc.abstractmethod
    async def set(self, namespace: str, key: str, data: typing.Any, ttl: int | None = None) -> None:
        """Store JSON-serialisable ``data`` under ``(namespace, key)`` with optional TTL in seconds."""

    @abc.abstractmethod
    async def delete(self, namespace: str, key: str) -> None:
        """Delete the entry at ``(namespace, key)``."""

    @abc.abstractmethod
    async def set_mapping(
        self,
        from_ns: str,
        from_key: str,
        to_ns: str,
        to_key: str,
        ttl: int | None = None,
    ) -> None:
        """Record a directional mapping from ``(from_ns, from_key)`` to ``to_key`` in ``to_ns``.

        Example: ``set_mapping('mcp_access_token', 'mcp_xxx', 'upstream_access_token', 'upstream_yyy')``.
        """

    @abc.abstractmethod
    async def get_mapping(self, from_ns: str, from_key: str, to_ns: str) -> typing.Any | None:
        """Return the target key in ``to_ns`` for ``(from_ns, from_key)``, or ``None``."""

    async def pop(self, namespace: str, key: str) -> typing.Any | None:
        """Delete the entry at ``(namespace, key)`` and return what it held, or ``None`` for none.

        A backend that can read and delete in one step overrides this, so two callers never both receive an entry.
        """
        data = await self.get(namespace, key)
        await self.delete(namespace, key)
        return data

    async def delete_mapping(self, from_ns: str, from_key: str, to_ns: str) -> None:
        """Delete the mapping from ``(from_ns, from_key)`` into ``to_ns``.

        A backend that cannot leaves the mapping to expire with the TTL it was set with.
        """
        return None

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources such as connections and file handles."""
