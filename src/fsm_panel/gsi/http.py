"""Loopback-only HTTP transport for observation-only GSI reception."""

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from typing import Self

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .receiver import GameStateReceiver, GsiError


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def create_gsi_app(receiver: GameStateReceiver, *, body_timeout: float = 5.0) -> FastAPI:
    """Create an ASGI app; use GsiServer to enforce the network bind boundary."""
    if not 0 < body_timeout <= 60:
        raise ValueError("invalid_body_timeout")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/")
    async def receive(request: Request) -> Response:
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = False
        if not local:
            return _error("loopback_required", 403)
        # Do not decompress untrusted data or accept ambiguous framing.
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _error("unsupported_content_encoding", 415)
        lengths = request.headers.getlist("content-length")
        expected_length: int | None = None
        if lengths:
            if (
                len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
                or len(lengths[0]) > 20
                or "transfer-encoding" in request.headers
            ):
                return _error("invalid_content_length", 400)
            expected_length = int(lengths[0])
            if expected_length > receiver.max_payload_bytes:
                return _error("payload_too_large", 413)
        body = bytearray()
        try:
            async with asyncio.timeout(body_timeout):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > receiver.max_payload_bytes:
                        return _error("payload_too_large", 413)
                    body.extend(chunk)
            if expected_length is not None and expected_length != len(body):
                return _error("invalid_content_length", 400)
            receiver.receive(bytes(body))
        except TimeoutError:
            return _error("body_timeout", 408)
        except ClientDisconnect:
            return _error("client_disconnected", 400)
        except GsiError as error:
            return _error(error.code, error.status_code)
        return Response(status_code=200, media_type="text/html")

    return app


class _EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The Qt/application owner, not a telemetry listener, owns process signals.
        yield


class GsiServer:
    """An explicitly started, owned IPv4 loopback server; construction does no I/O."""

    def __init__(
        self,
        receiver: GameStateReceiver,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        body_timeout: float = 5.0,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("gsi_bind_must_be_127_0_0_1")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid_gsi_port")
        self._app = create_gsi_app(receiver, body_timeout=body_timeout)
        self._host, self._requested_port = host, port
        self._server: _EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def port(self) -> int:
        if self._socket is None or self._task is None or self._task.done():
            raise RuntimeError("gsi_server_not_running")
        return int(self._socket.getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    async def start(self) -> Self:
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return self
            await self._close()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind((self._host, self._requested_port))
                listener.setblocking(False)
            except OSError:
                listener.close()
                raise RuntimeError("gsi_bind_failed") from None
            self._socket = listener
            self._server = _EmbeddedServer(
                uvicorn.Config(
                    self._app,
                    host=self._host,
                    port=self._requested_port,
                    access_log=False,
                    log_config=None,
                    proxy_headers=False,
                    server_header=False,
                    lifespan="off",
                    ws="none",
                    http="h11",
                    timeout_keep_alive=2,
                    timeout_graceful_shutdown=2,
                    limit_concurrency=100,
                    h11_max_incomplete_event_size=16 * 1024,
                )
            )
            self._task = asyncio.create_task(self._server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not self._server.started:
                        if self._task.done():
                            raise RuntimeError("gsi_start_failed")
                        await asyncio.sleep(0.01)
            except BaseException:
                await self._close()
                raise
            return self

    async def _close(self) -> None:
        task, server, listener = self._task, self._server, self._socket
        self._task = self._server = self._socket = None
        try:
            if task is not None:
                if server is not None:
                    server.should_exit = True
                try:
                    async with asyncio.timeout(5):
                        await asyncio.shield(task)
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
        finally:
            if listener is not None:
                listener.close()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
