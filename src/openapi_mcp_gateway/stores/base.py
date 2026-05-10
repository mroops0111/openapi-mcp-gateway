import abc
import typing


class TokenStore(abc.ABC):
    """Namespaced key-value persistence with TTL and directional namespace mappings.

    Used to correlate MCP OAuth artefacts with upstream API credentials.
    Common namespaces: ``mcp_client``, ``mcp_access_token``, ``mcp_refresh_token``,
    ``mcp_auth_code``, ``mcp_auth_state``, ``api_access_token``.
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

        Example: ``set_mapping('mcp_access_token', 'mcp_xxx', 'api_access_token', 'api_yyy')``.
        """

    @abc.abstractmethod
    async def get_mapping(self, from_ns: str, from_key: str, to_ns: str) -> typing.Any | None:
        """Return the target key in ``to_ns`` for ``(from_ns, from_key)``, or ``None``."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources (connections, file handles, etc.)."""
