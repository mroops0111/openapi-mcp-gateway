import contextlib
import socket
import typing

import anyio
import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> typing.AsyncIterator[str]:
    """Serve ``app`` with uvicorn on a free loopback port and yield its base URL."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
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
            yield f'http://127.0.0.1:{sock.getsockname()[1]}'
        finally:
            server.should_exit = True
