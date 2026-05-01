import abc
import typing


class TokenStore(abc.ABC):
    """Abstract base class for pluggable token storage backends.

    Stores key-value data with optional TTL, and supports
    directional mappings between namespaces (e.g. mcp_access_token → api_access_token).
    """

    @abc.abstractmethod
    async def get(self, namespace: str, key: str) -> typing.Any | None:
        """Get data by namespace and key.

        Args:
            namespace: e.g. 'mcp_client', 'mcp_access_token', 'api_access_token'
            key: unique identifier within the namespace
        """

    @abc.abstractmethod
    async def set(self, namespace: str, key: str, data: typing.Any, ttl: int | None = None) -> None:
        """Store data with optional TTL.

        Args:
            namespace: e.g. 'mcp_access_token'
            key: unique identifier
            data: JSON-serializable data
            ttl: time-to-live in seconds, None for no expiry
        """

    @abc.abstractmethod
    async def delete(self, namespace: str, key: str) -> None:
        """Delete data by namespace and key."""

    @abc.abstractmethod
    async def set_mapping(
        self,
        from_ns: str,
        from_key: str,
        to_ns: str,
        to_key: str,
        ttl: int | None = None,
    ) -> None:
        """Create a directional mapping: (from_ns, from_key) → to_key in to_ns.

        Example: set_mapping('mcp_access_token', 'mcp_xxx', 'api_access_token', 'api_yyy')
        """

    @abc.abstractmethod
    async def get_mapping(self, from_ns: str, from_key: str, to_ns: str) -> typing.Any | None:
        """Resolve a directional mapping.

        Returns the target key in to_ns, or None if not found.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources (connections, file handles, etc.)."""
