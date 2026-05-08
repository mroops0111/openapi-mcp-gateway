from .base import TokenStore
from .memory import MemoryTokenStore


_RedisTokenStore: type[TokenStore] | None
try:
    from .redis import RedisTokenStore as _RedisTokenStore
except ImportError:
    _RedisTokenStore = None


__all__ = ['MemoryTokenStore', 'TokenStore']


def create_store(store_type: str = 'memory', **kwargs) -> TokenStore:
    """Instantiate the configured ``TokenStore`` backend.

    Args:
        store_type: Either ``memory`` or ``redis``.
        **kwargs: Forwarded to ``RedisTokenStore`` (for example ``url``, ``prefix``).

    Returns:
        A concrete ``TokenStore`` implementation.
    """
    if store_type == 'memory':
        return MemoryTokenStore()
    if store_type == 'redis':
        if _RedisTokenStore is None:
            raise ImportError(
                'Redis store requires the redis package. Install with: pip install openapi-mcp-gateway[redis]'
            )
        return _RedisTokenStore(**kwargs)
    raise ValueError(f'Unknown store type: {store_type}. Supported: memory, redis')
