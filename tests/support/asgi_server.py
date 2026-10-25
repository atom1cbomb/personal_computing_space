"""Real loopback ASGI servers for transport and connection-lifetime tests."""
import asyncio
import socket
from contextlib import asynccontextmanager

import uvicorn


@asynccontextmanager
async def running_asgi(app, **options):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="websockets",
                            ws_max_size=65536, ws_max_queue=16, timeout_graceful_shutdown=2, **options))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError("test_server_failed")
                await asyncio.sleep(.01)
        scheme = "https" if options.get("ssl_certfile") else "http"
        yield f"{scheme}://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
