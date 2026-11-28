import typing

from openapi_mcp_gateway.exposure._upstream_http import UpstreamSharedHttpClient
from openapi_mcp_gateway.gateway import Gateway


def auth_shutdown_hooks(gateway: Gateway) -> list[typing.Callable[[], typing.Awaitable[None]]]:
    """The shutdown hooks an auth flow registered, leaving out the one closing each server's upstream client."""
    return [
        hook
        for hook in gateway._shutdown_hooks
        if not isinstance(getattr(hook, '__self__', None), UpstreamSharedHttpClient)
    ]
