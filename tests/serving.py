import contextlib
import socket
import typing

import anyio
import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def serve(app: FastAPI, sock: socket.socket | None = None) -> typing.AsyncIterator[str]:
    """Serve ``app`` with uvicorn on ``sock``, or on a free loopback port, and yield its base URL."""
    if sock is None:
        sock = bound_socket()
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
            yield url_of(sock)
        finally:
            server.should_exit = True


def bound_socket() -> socket.socket:
    """A socket bound to a free loopback port, for a test that needs the URL before it builds the app."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    return sock


def url_of(sock: socket.socket) -> str:
    """The base URL ``sock`` serves at."""
    return f'http://127.0.0.1:{sock.getsockname()[1]}'
