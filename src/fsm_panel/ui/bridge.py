"""Asyncio worker thread with Qt-only cross-thread notifications."""

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal


class AsyncBridge(QObject):
    event = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    license_state = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="fsm-coordinator", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("coordinator_start_timeout")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def submit(self, operation: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        if self._closed:
            operation.close()
            raise RuntimeError("coordinator_closed")
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)

        def done(result: concurrent.futures.Future) -> None:
            try:
                value = result.result()
            except concurrent.futures.CancelledError:
                self.failed.emit("operation_cancelled")
            except Exception as error:
                # External exception strings may contain credentials or URLs.
                self.failed.emit(type(error).__name__)
            else:
                self.completed.emit(value)

        future.add_done_callback(done)
        return future

    def shutdown(self, cleanup: Coroutine[Any, Any, Any]) -> None:
        if self._closed:
            cleanup.close()
            return
        self._closed = True
        future = asyncio.run_coroutine_threadsafe(cleanup, self.loop)
        try:
            future.result(timeout=5)
        except TimeoutError, Exception:
            future.cancel()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("coordinator_shutdown_timeout")
