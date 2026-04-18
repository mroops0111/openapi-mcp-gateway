from .base import TokenStore
from .memory import MemoryTokenStore


__all__ = ['MemoryTokenStore', 'TokenStore']


def create_store(store_type: str = 'memory', **kwargs) -> TokenStore:
    """Factory function to create a TokenStore instance.

    Args:
        store_type: 'memory' or 'redis'
        **kwargs: Passed to the store constructor (e.g. url, prefix for Redis)

    Returns:
        A TokenStore-compatible instance.
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
