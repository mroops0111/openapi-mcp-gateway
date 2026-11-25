import contextlib
import socket
import typing

import anyio
import uvicorn
from starlette.types import ASGIApp

from openapi_mcp_gateway.gateway import Gateway


class ServingAddress(typing.NamedTuple):
    """A loopback socket an app will be served on, with the base URL it serves at, settled before the app is built."""

    sock: socket.socket
    url: str


@contextlib.asynccontextmanager
async def serve(app: ASGIApp, sock: socket.socket | None = None) -> typing.AsyncIterator[str]:
    """Serve ``app`` with uvicorn on ``sock``, or on a free loopback port, and yield its base URL."""
    if sock is None:
        sock = bind_loopback_socket()
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, ws='none'))
    served = anyio.Event()

    async def run_server() -> None:
        # ``Server.serve`` returns rather than raising when the lifespan fails, so record that it finished.
        await server.serve([sock])
        served.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_server)
        with anyio.fail_after(5):
            while not server.started and not served.is_set():
                await anyio.sleep(0.01)
        assert server.started, 'the app failed to start'
        try:
            yield build_socket_url(sock)
        finally:
            server.should_exit = True


@contextlib.asynccontextmanager
async def serve_gateway(
    build: typing.Callable[[str], Gateway],
    app_of: typing.Callable[[Gateway], ASGIApp] | None = None,
    address: ServingAddress | None = None,
) -> typing.AsyncIterator[str]:
    """Serve the gateway ``build`` makes for the address it is served at, and yield that address.

    The address is settled before the gateway is built, since the gateway publishes it in its documents.
    ``address`` is that address when a test settled it earlier, and a free loopback port otherwise.
    ``app_of`` turns the gateway into the app that is served, its own streamable HTTP app by default.
    """
    if address is None:
        address = bind_serving_address()
    gateway = build(address.url)
    app = app_of(gateway) if app_of is not None else gateway._build_app(transport='streamable-http')
    async with serve(app, address.sock):
        yield address.url


def bind_serving_address() -> ServingAddress:
    """A free loopback address, for a test that needs the URL before it builds the app."""
    sock = bind_loopback_socket()
    return ServingAddress(sock, build_socket_url(sock))


def bind_loopback_socket() -> socket.socket:
    """A socket bound to a free loopback port, for a test that needs the URL before it builds the app."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    return sock


def build_socket_url(sock: socket.socket) -> str:
    """The base URL ``sock`` serves at."""
    return f'http://127.0.0.1:{sock.getsockname()[1]}'
