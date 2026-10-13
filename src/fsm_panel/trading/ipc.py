"""Owned subprocess transport: JSON stdout, drained and discarded diagnostic stderr."""

import asyncio
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol

from .models import PUBLIC_CODES, TradeError, WorkerRejected

PROTOCOL_VERSION = 1
MAX_REQUEST = 65_536
MAX_RESPONSE = 4_194_304


class WorkerClient(Protocol):
    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int = 30_000,
        admitted_until_ms: int | None = None,
    ) -> dict[str, Any]: ...


class NodeWorkerClient:
    """One owned worker per account/session; construct on its asyncio event loop.

    Command arguments may contain runtime/script/database/scenario paths only. Never
    use a shell or put credentials in command arguments or environment variables.
    """

    def __init__(self, command: Sequence[str]) -> None:
        self.command = tuple(command)
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._writes = asyncio.Lock()
        self._starts = asyncio.Lock()
        self._broken = False
        self.diagnostic_bytes = 0
        self._admitted_requests: dict[str, int] = {}
        self._last_renewal = 0.0

    async def __aenter__(self) -> NodeWorkerClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        async with self._starts:
            if self._broken:
                raise TradeError("worker_disconnected")
            if self._process is not None:
                return
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_RESPONSE + 1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except OSError, ValueError:
                raise TradeError("worker_start_failed") from None
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        while chunk := await self._process.stderr.read(8192):
            # No raw diagnostics are retained, surfaced to GUI, or added to exceptions.
            self.diagnostic_bytes += len(chunk)

    def _fail(self, code: str) -> None:
        self._broken = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TradeError(code))

    async def _read(self) -> None:
        assert self._process and self._process.stdout
        try:
            while line := await self._process.stdout.readline():
                if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
                    raise ValueError
                response = json.loads(line)
                if (
                    not isinstance(response, dict)
                    or response.get("version") != PROTOCOL_VERSION
                    or type(response.get("ok")) is not bool
                ):
                    raise ValueError
                request_id = response.get("request_id")
                if not isinstance(request_id, str) or request_id not in self._pending:
                    raise ValueError
                future = self._pending[request_id]
                if future.done():
                    raise ValueError
                if response["ok"]:
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise ValueError
                    future.set_result(result)
                else:
                    code = response.get("error", {}).get("code")
                    future.set_exception(
                        WorkerRejected(
                            code
                            if isinstance(code, str) and code in PUBLIC_CODES
                            else "worker_error"
                        )
                    )
            self._fail("worker_disconnected")
        except ValueError, TypeError, UnicodeError, AttributeError:
            self._fail("invalid_worker_response")
            if self._process.returncode is None:
                self._process.kill()
        except OSError:
            self._fail("worker_disconnected")

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_ms: int = 30_000,
        admitted_until_ms: int | None = None,
    ) -> dict[str, Any]:
        await self.start()
        if self._broken or not self._process or self._process.returncode is not None:
            raise TradeError("worker_disconnected")
        request_id = str(uuid.uuid4())
        request: dict[str, Any] = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": method,
            "timeout_ms": timeout_ms,
            "params": params,
        }
        if admitted_until_ms is not None:
            request["admission"] = {"expires_at_ms": admitted_until_ms}
        try:
            frame = json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf8") + b"\n"
        except TypeError, ValueError:
            raise TradeError("invalid_request") from None
        if len(frame) - 1 > MAX_REQUEST:
            raise TradeError("frame_too_large")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if admitted_until_ms is not None and method in {"send", "inventory", "activity"}:
            self._admitted_requests[request_id] = admitted_until_ms
        assert self._process.stdin
        try:
            async with self._writes:
                self._process.stdin.write(frame)
                await self._process.stdin.drain()
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_ms / 1000 + 2)
        except asyncio.CancelledError:
            if method != "cancel":
                cleanup = asyncio.create_task(self._cancel_and_drain(request_id, future))
                # Repeated UI cancellation cannot interrupt durable settlement/owned cleanup.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
            raise
        except TimeoutError:
            if method != "cancel":
                await self._cancel_and_drain(request_id, future)
            raise TradeError("worker_timeout") from None
        except BrokenPipeError, ConnectionError, OSError:
            self._fail("worker_disconnected")
            raise TradeError("worker_disconnected") from None
        finally:
            self._pending.pop(request_id, None)
            self._admitted_requests.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # consume an abandoned response exception

    async def renew_admission(self, deadline: int) -> None:
        # Local admission is checked every 250ms; a 5s IPC cadence bounds request-ID
        # history during 24h activity. Short leases bypass throttling near expiry.
        expiring = any(
            until <= time.time() * 1000 + 1000 for until in self._admitted_requests.values()
        )
        if not expiring and time.monotonic() - self._last_renewal < 5:
            return
        self._last_renewal = time.monotonic()
        for request_id in tuple(self._admitted_requests):
            await self.request(
                "renew",
                {"target_request_id": request_id},
                timeout_ms=1000,
                admitted_until_ms=deadline,
            )
            if request_id in self._admitted_requests:
                self._admitted_requests[request_id] = deadline

    async def _cancel_and_drain(self, request_id: str, future: asyncio.Future) -> None:
        try:
            await self.request("cancel", {"target_request_id": request_id}, timeout_ms=500)
            await asyncio.wait_for(asyncio.shield(future), timeout=1)
        except TradeError, TimeoutError:
            await self.close()

    async def close(self) -> None:
        self._broken = True
        if self._process is None:
            return
        if self._process.stdin:
            self._process.stdin.close()
        if self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=1)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        for task in (self._reader, self._stderr):
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1)
                except TimeoutError:
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._fail("worker_disconnected")
