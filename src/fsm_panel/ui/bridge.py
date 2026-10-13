"""Asyncio worker thread with Qt-only cross-thread notifications."""

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot


class AsyncBridge(QObject):
    domain_event = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    license_state = Signal(str)
    cleanup_finished = Signal()
    shutdown_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self.shutdown_error: str | None = None
        self.cleanup_finished.connect(self._finish_shutdown, Qt.ConnectionType.QueuedConnection)
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

    def request_shutdown(self, cleanup: Coroutine[Any, Any, Any]) -> None:
        """Keep the Qt event loop responsive while bounded adapter cleanup runs."""
        if self._closed:
            cleanup.close()
            return
        self._closed = True
        future = asyncio.run_coroutine_threadsafe(cleanup, self.loop)

        def cleaned(result: concurrent.futures.Future) -> None:
            try:
                result.result()
            except Exception as error:
                self.shutdown_error = type(error).__name__
            self.cleanup_finished.emit()

        future.add_done_callback(cleaned)

    @Slot()
    def _finish_shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("coordinator_shutdown_timeout")
        self.shutdown_finished.emit()
