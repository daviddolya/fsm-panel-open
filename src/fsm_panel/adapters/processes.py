"""Process ownership infrastructure; never discovers/kills processes by image name."""

import asyncio
import math
import os
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psutil

from fsm_panel.core.models import StateConflict, ValidationError
from fsm_panel.core.repository import AccountRepository


@dataclass(frozen=True, slots=True)
class OwnedProcess:
    token: str
    account_id: str
    pid: int
    create_time: float
    executable: str = field(repr=False)


class ProcessHandle(Protocol):
    pid: int

    def create_time(self) -> float: ...

    def exe(self) -> str: ...

    def children(self, recursive: bool = False) -> list[Any]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> Any: ...


class OwnedProcessController:
    """Starts explicit argv with shell=False; persists PID + birth time + executable.

    A recovered record is a candidate, never proof by itself. Before signaling any
    process, identity is rechecked. A reused PID is forgotten without being killed.
    Descendants are registered only while a verified owned parent still exists.
    """

    def __init__(
        self, repository: AccountRepository, *,
        spawn: Callable[..., ProcessHandle] | None = None,
        lookup: Callable[[int], ProcessHandle] | None = None,
        stop_timeout: float = 3.0,
    ) -> None:
        if not math.isfinite(stop_timeout) or not 0 < stop_timeout <= 60:
            raise ValidationError("invalid_process_timeout")
        self.repository = repository
        self._spawn = spawn or psutil.Popen
        self._lookup = lookup or psutil.Process
        self.stop_timeout = stop_timeout
        self._lock = asyncio.Lock()

    @staticmethod
    def _executable(path: str) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    @staticmethod
    def _from_record(record: dict[str, object]) -> OwnedProcess:
        return OwnedProcess(
            str(record["token"]), str(record["account_id"]), int(str(record["pid"])),
            float(str(record["create_time"])), str(record["executable"]),
        )

    def _remember(self, account_id: str, process: ProcessHandle) -> OwnedProcess:
        owned = OwnedProcess(str(uuid.uuid4()), account_id, process.pid, process.create_time(),
                             self._executable(process.exe()))
        self.repository.remember_process(owned.token, owned.account_id, owned.pid,
                                         owned.create_time, owned.executable)
        return owned

    def _verified(self, owned: OwnedProcess) -> ProcessHandle | None:
        try:
            process = self._lookup(owned.pid)
            if (abs(process.create_time() - owned.create_time) > 0.00001
                    or self._executable(process.exe()) != owned.executable):
                self.repository.forget_process(owned.token)
                return None
            return process
        except psutil.NoSuchProcess:
            self.repository.forget_process(owned.token)
            return None

    async def launch(self, account_id: str, argv: Sequence[str], *,
                     cwd: str | Path | None = None) -> OwnedProcess:
        self.repository.get(account_id)
        if (isinstance(argv, (str, bytes)) or not argv
                or any(type(argument) is not str or "\x00" in argument for argument in argv)):
            raise ValidationError("invalid_process_argv")
        executable = Path(argv[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValidationError("absolute_executable_required")
        if os.name == "nt" and executable.suffix.lower() not in {".exe", ".com"}:
            raise ValidationError("windows_executable_required")
        if cwd is not None and not Path(cwd).is_dir():
            raise ValidationError("invalid_process_directory")
        async with self._lock:
            launch = asyncio.create_task(asyncio.to_thread(
                self._launch_sync, account_id, list(argv), cwd
            ))
            try:
                return await asyncio.shield(launch)
            except asyncio.CancelledError:
                # Cancelling to_thread cannot stop its thread. Wait until ownership is
                # recorded, then clean up before allowing the caller to leave.
                while not launch.done():
                    try:
                        await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        continue
                if not launch.cancelled() and launch.exception() is None:
                    cleanup = asyncio.create_task(asyncio.to_thread(self._stop_sync, account_id))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
                raise

    def _launch_sync(self, account_id: str, argv: list[str],
                     cwd: str | Path | None) -> OwnedProcess:
        process: ProcessHandle | None = None
        try:
            process = self._spawn(
                argv, shell=False, cwd=str(cwd) if cwd else None,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                close_fds=True,
            )
            return self._remember(account_id, process)
        except Exception:
            if process is not None:
                # This exact handle was created here; clean up if persistence failed.
                try:
                    process.terminate()
                    process.wait(timeout=self.stop_timeout)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            raise StateConflict("owned_process_launch_failed") from None

    def _collect_children(self, owned: OwnedProcess) -> None:
        parent = self._verified(owned)
        if parent is None:
            return
        known = {(record["pid"], record["create_time"])
                 for record in self.repository.owned_processes()}
        try:
            children = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        # Recheck the parent after enumeration; ancestry of a reused PID is not ownership.
        if self._verified(owned) is None:
            return
        for child in children:
            try:
                identity = (child.pid, child.create_time())
                if identity not in known:
                    self._remember(owned.account_id, child)
                    known.add(identity)
            except psutil.NoSuchProcess:
                continue

    def _stop_sync(self, account_id: str) -> None:
        records = [self._from_record(record)
                   for record in self.repository.owned_processes(account_id)]
        errors = False
        for owned in records:
            try:
                self._collect_children(owned)
            except (psutil.AccessDenied, OSError):
                errors = True
        # Children are inserted after parents; reverse order terminates leaves first.
        records = [self._from_record(record)
                   for record in self.repository.owned_processes(account_id)]
        for owned in reversed(records):
            try:
                process = self._verified(owned)
                if process is None:
                    continue
                process.terminate()
                try:
                    process.wait(timeout=self.stop_timeout)
                except psutil.TimeoutExpired:
                    process = self._verified(owned)
                    if process is not None:
                        process.kill()
                        process.wait(timeout=self.stop_timeout)
                self.repository.forget_process(owned.token)
            except psutil.NoSuchProcess:
                self.repository.forget_process(owned.token)
            except (psutil.AccessDenied, psutil.TimeoutExpired, OSError):
                # Retain records for explicit retry or next-start recovery.
                errors = True
        if errors:
            raise StateConflict("owned_process_cleanup_required")

    async def stop(self, account_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._stop_sync, account_id)

    async def recover(self) -> None:
        for account_id in {str(record["account_id"])
                           for record in self.repository.owned_processes()}:
            await self.stop(account_id)
