import contextlib
import socket
import typing

import anyio
import uvicorn


# How long an app may take to start before serving it counts as failed.
STARTUP_TIMEOUT_SECONDS = 10


class ServerStartError(RuntimeError):
    """An app served for a benchmark stopped before it started listening."""


@contextlib.asynccontextmanager
async def serve(app: typing.Any) -> typing.AsyncIterator[str]:
    """Serve the ASGI ``app`` on a free loopback port for as long as the block runs, and yield its base URL.

    A model's agent framework reaches the gateway over HTTP, as a real client would, so the gateway is served for real.
    """
    listening_socket = socket.socket()
    listening_socket.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level='warning', ws='none'))
    stopped = anyio.Event()

    async def run_server() -> None:
        # Server.serve returns rather than raising when the app's lifespan fails, so its end is recorded instead.
        await server.serve([listening_socket])
        stopped.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_server)
        with anyio.fail_after(STARTUP_TIMEOUT_SECONDS):
            while not server.started and not stopped.is_set():
                await anyio.sleep(0.01)
        if not server.started:
            raise ServerStartError('The app stopped before it started listening')
        try:
            yield f'http://127.0.0.1:{listening_socket.getsockname()[1]}'
        finally:
            server.should_exit = True
