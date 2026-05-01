from .base import TokenStore
from .memory import MemoryTokenStore


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
        try:
            from .redis import RedisTokenStore
        except ImportError:
            raise ImportError(  # noqa: B904
                'Redis store requires the redis package. Install with: pip install openapi-mcp-gateway[redis]'
            )
        return RedisTokenStore(**kwargs)
    raise ValueError(f'Unknown store type: {store_type}. Supported: memory, redis')
