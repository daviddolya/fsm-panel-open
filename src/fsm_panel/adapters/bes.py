"""Ownership-scoped BES 1.7.9 transport. See docs/bes.md for evidence and limits.

No BES binary/source is distributed here. No image-name discovery, broadcast,
forced termination, suspend, or resume operation is provided by this adapter.
"""

import asyncio
import ctypes
import hashlib
import json
import math
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import psutil

from fsm_panel.adapters.processes import OwnedProcess
from fsm_panel.adapters.windows import WindowsWindowLookup
from fsm_panel.core.models import StateConflict, ValidationError
from fsm_panel.core.repository import AccountRepository

SHIPPED_BES_SHA256 = "8b10f04516ddfd970580b19171aa15f9205a8e0c12c4312a6d2a88f4a565eb1f"
BES_VERSION = "1.7.9"
BES_WINDOW_CLASS = "BATTLEENC"
BES_COPYDATA_ID = 0xBA1EC0DE
BES_COMMAND_EXIT = 0xBECD0000
BES_COMMAND_PARSE = 0xBECD0001
BES_STOP_COMMAND = 30004
BES_COPYDATA_SIZE = 16 + 1024 * 2


class BesError(StateConflict):
    """Stable safe code; retained sessions explain which ownership needs recovery."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


@dataclass(frozen=True, slots=True)
class ReviewedBesExecutable:
    path: str | Path = field(repr=False)
    sha256: str = SHIPPED_BES_SHA256


def verify_executable(executable: ReviewedBesExecutable) -> Path:
    """Read-only validation; construction never launches or probes the program."""
    path = Path(executable.path)
    if (
        executable.sha256.lower() != SHIPPED_BES_SHA256
        or not path.is_absolute()
        or path.suffix.lower() != ".exe"
        or not path.is_file()
    ):
        raise BesError("bes_reviewed_executable_required")
    try:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise BesError("bes_executable_unreadable") from None
    if digest != SHIPPED_BES_SHA256:
        raise BesError("bes_executable_hash_mismatch")
    return path.resolve()


def launch_argv(path: str | Path) -> tuple[str, ...]:
    """Idle launch: --allow-multi alone does NOT prevent the legacy broadcast."""
    return (str(path), "--selfish", "--allow-multi", "--minimize")


def limit_command(pid: int, reduction_percent: int) -> str:
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise ValidationError("bes_invalid_pid")
    if type(reduction_percent) is not int or not 1 <= reduction_percent <= 99:
        raise ValidationError("bes_invalid_reduction_percent")
    # Explicit 100-ms cycle, zero initial delay: deployment INI cannot change these.
    return f'"BES.exe" "PID:{pid}" {reduction_percent};100;0'


def copydata_payload(command: str = "", *, exit_now: bool = False) -> bytes:
    if not isinstance(command, str) or "\x00" in command:
        raise ValidationError("bes_invalid_command")
    try:
        encoded = command.encode("utf-16-le")
    except UnicodeEncodeError:
        raise ValidationError("bes_invalid_command") from None
    if len(encoded) > 2046:
        raise ValidationError("bes_command_too_long")
    return struct.pack("<IIII", BES_COMMAND_EXIT if exit_now else BES_COMMAND_PARSE, 0, 0, 0) + (
        encoded + bytes(2048 - len(encoded))
    )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time: float
    executable: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BesWindow:
    handle: int
    pid: int
    class_name: str = BES_WINDOW_CLASS


ThreadIdentity = tuple[int, int]  # TID and raw creation FILETIME; protects against TID reuse.


class BesDriver(Protocol):
    """Synchronous bounded transport; tests inject a synthetic implementation.

    guard must pin/recheck PID, birth time and image while inside the context.
    resumed is read-only: True only if all threads were observed unsuspended;
    False/None never authorizes exit. Transport must recheck HWND owner at send.
    """

    def spawn(self, argv: Sequence[str]) -> ProcessIdentity: ...
    def identity(self, pid: int) -> ProcessIdentity | None: ...
    def guard(self, process: OwnedProcess) -> AbstractContextManager[bool]: ...
    def windows(self, pid: int) -> list[BesWindow]: ...
    def window(self, handle: int) -> BesWindow | None: ...
    def threads(self, pid: int) -> tuple[ThreadIdentity, ...]: ...
    def resumed(self, target: OwnedProcess) -> bool | None: ...
    def send_copydata(self, window: BesWindow, payload: bytes, timeout: float) -> int: ...
    def send_stop(self, window: BesWindow, timeout: float) -> None: ...


@dataclass(frozen=True, slots=True)
class BesSession:
    token: str
    target: OwnedProcess
    instance: OwnedProcess
    sha256: str = SHIPPED_BES_SHA256
    window_handle: int | None = None
    baseline_threads: tuple[ThreadIdentity, ...] = ()
    reduction_percent: int | None = None
    state: str = "idle"
    may_have_limited: bool = False
    error_code: str | None = None

    @property
    def account_id(self) -> str:
        return self.target.account_id

    @property
    def session_id(self) -> str:
        return self.token


class BesJournal:
    """Separate durable ownership journal; never a generic process kill list.

    One journal owner per file is enforced by an OS lock. Each mutation commits
    before the following external action; a crash cannot erase an in-flight limit.
    Paths/identities are private operational data and must not enter exports.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._lock_file: Any = None
        try:
            if str(path) != ":memory:":
                path = Path(path).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                self._lock_file = Path(str(path) + ".lock").open("a+b")
                self._lock_file.seek(0)
                if not self._lock_file.read(1):
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._db = sqlite3.connect(str(path), check_same_thread=False)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS bes_sessions (token TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            self._db.commit()
        except OSError, sqlite3.Error:
            if self._lock_file:
                self._lock_file.close()
            raise BesError("bes_journal_unavailable_or_busy") from None

    def __enter__(self) -> BesJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None

    @contextmanager
    def operation(self) -> Iterator[None]:
        with self._operation_lock:
            yield

    def save(self, session: BesSession) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO bes_sessions VALUES (?, ?)",
                (session.token, json.dumps(asdict(session))),
            )

    def remove(self, token: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM bes_sessions WHERE token=?", (token,))

    def sessions(self, account_id: str | None = None) -> list[BesSession]:
        with self._lock:
            rows = self._db.execute(
                "SELECT token,payload FROM bes_sessions ORDER BY rowid"
            ).fetchall()
        sessions = []
        try:
            for token, payload in rows:
                data = json.loads(payload)
                data["target"] = OwnedProcess(**data["target"])
                data["instance"] = OwnedProcess(**data["instance"])
                data["baseline_threads"] = tuple(tuple(item) for item in data["baseline_threads"])
                session = BesSession(**data)
                if (
                    session.token != token
                    or session.sha256 != SHIPPED_BES_SHA256
                    or session.account_id != session.instance.account_id
                    or session.state
                    not in {"idle", "delivered", "unknown", "cleanup_required", "resumed"}
                    or type(session.may_have_limited) is not bool
                    or not _valid_identity(session.target)
                    or not _valid_identity(session.instance)
                    or (
                        session.window_handle is not None
                        and (type(session.window_handle) is not int or session.window_handle <= 0)
                    )
                    or any(
                        len(item) != 2 or any(type(n) is not int or n <= 0 for n in item)
                        for item in session.baseline_threads
                    )
                ):
                    raise ValueError
                if account_id is None or session.account_id == account_id:
                    sessions.append(session)
        except ValueError, KeyError, TypeError:
            raise BesError("bes_journal_invalid") from None
        return sessions


def _valid_identity(process: OwnedProcess | ProcessIdentity) -> bool:
    return (
        type(process.pid) is int
        and 0 < process.pid <= 0xFFFFFFFF
        and type(process.create_time) in {float, int}
        and math.isfinite(process.create_time)
        and process.create_time > 0
        and isinstance(process.executable, str)
        and Path(process.executable).is_absolute()
    )


def _same(owned: OwnedProcess, current: ProcessIdentity | None) -> bool:
    return bool(
        current
        and _valid_identity(current)
        and _valid_identity(owned)
        and owned.pid == current.pid
        and abs(owned.create_time - current.create_time) <= 0.00001
        and _path(owned.executable) == _path(current.executable)
    )


T = TypeVar("T")


class OwnedBesService:
    """Serializes operations; cancellation finishes ownership/cleanup before release.

    An executable is required only for a new instance. Recovery remains available
    with missing/changed launch configuration. stop/recover never force termination.
    """

    def __init__(
        self,
        repository: AccountRepository,
        executable: ReviewedBesExecutable | None,
        journal: BesJournal,
        *,
        driver: BesDriver | None = None,
        startup_timeout: float = 5.0,
        command_timeout: float = 3.0,
        poll_interval: float = 0.05,
    ) -> None:
        if any(
            not math.isfinite(value) or not 0 < value <= 60
            for value in (startup_timeout, command_timeout, poll_interval)
        ):
            raise ValidationError("bes_invalid_timeout")
        self.repository, self.executable, self.journal = repository, executable, journal
        self.driver = driver or WindowsBesDriver()
        self.startup_timeout, self.command_timeout = startup_timeout, command_timeout
        self.poll_interval = poll_interval
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        # If a disk fault prevents persistence after idle spawn, retain the exact
        # new identity in memory for retry. No limit is sent before durable save.
        self._unsaved: dict[str, BesSession] = {}

    def sessions(self, account_id: str | None = None) -> list[BesSession]:
        with self._state_lock:
            result = {s.token: s for s in self.journal.sessions(account_id)}
            result.update(
                {
                    k: s
                    for k, s in self._unsaved.items()
                    if account_id is None or s.account_id == account_id
                }
            )
            return list(result.values())

    async def _run(self, operation: Callable[[], T], cancel_cleanup: str | None = None) -> T:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(self._operation, operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                # Even if the in-flight call failed, its effects may have happened.
                failure = None if task.cancelled() else task.exception()
                if cancel_cleanup is not None:
                    cleanup = asyncio.create_task(
                        asyncio.to_thread(
                            self._operation, lambda: self._stop_account(cancel_cleanup)
                        )
                    )
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            break
                    cleanup.result()  # Actionable cleanup error takes precedence.
                if failure is not None:
                    raise failure  # Surface a cleanup error before cancellation.
                raise

    def _operation(self, operation: Callable[[], T]) -> T:
        with self.journal.operation():
            try:
                return operation()
            except BesError, ValidationError:
                raise
            except Exception:
                raise BesError("bes_operation_failed_cleanup_required") from None

    async def limit(
        self,
        target: OwnedProcess,
        reduction_percent: int,
        *,
        executable: ReviewedBesExecutable | None = None,
    ) -> BesSession:
        limit_command(target.pid, reduction_percent)
        return await self._run(
            lambda: self._limit(target, reduction_percent, executable), target.account_id
        )

    async def unlimit(self, target: OwnedProcess) -> None:
        await self._run(lambda: self._unlimit_target(target))

    async def stop(self, account_id: str) -> None:
        await self._run(lambda: self._stop_account(account_id))

    async def recover(self) -> None:
        await self._run(lambda: self._stop_account(None))

    def _save(self, session: BesSession) -> BesSession:
        with self._state_lock:
            self._unsaved[session.token] = session
            try:
                self.journal.save(session)
            except Exception:
                raise BesError("bes_journal_write_failed_cleanup_required") from None
            self._unsaved.pop(session.token, None)
        return session

    def _error(self, session: BesSession, code: str) -> None:
        self._save(replace(session, state="cleanup_required", error_code=code))
        raise BesError(code)

    def _owned_target(self, target: OwnedProcess) -> None:
        if not _valid_identity(target) or not any(
            record == asdict(target)
            for record in self.repository.owned_processes(target.account_id)
        ):
            raise BesError("bes_target_not_owned")
        if not _same(target, self.driver.identity(target.pid)):
            raise BesError("bes_target_identity_changed")

    def _window(self, session: BesSession) -> BesWindow:
        if not _same(session.instance, self.driver.identity(session.instance.pid)):
            raise BesError("bes_instance_identity_changed")
        window: BesWindow | None
        if session.window_handle is None:
            matches = [
                w
                for w in self.driver.windows(session.instance.pid)
                if w.pid == session.instance.pid and w.class_name == BES_WINDOW_CLASS
            ]
            if len(matches) != 1:
                raise BesError("bes_window_unavailable")
            window = matches[0]
        else:
            window = self.driver.window(session.window_handle)
        if (
            window is None
            or window.pid != session.instance.pid
            or window.class_name != BES_WINDOW_CLASS
            or window.handle <= 0
        ):
            raise BesError("bes_window_ownership_changed")
        return window

    def _new(
        self,
        target: OwnedProcess,
        executable: ReviewedBesExecutable | None,
    ) -> BesSession:
        executable = executable or self.executable
        if executable is None:
            raise BesError("bes_reviewed_executable_required")
        path = verify_executable(executable)
        identity = self.driver.spawn(launch_argv(path))
        if not _valid_identity(identity) or _path(identity.executable) != _path(path):
            raise BesError("bes_spawn_identity_invalid")
        instance = OwnedProcess(
            str(uuid.uuid4()),
            target.account_id,
            identity.pid,
            identity.create_time,
            _path(identity.executable),
        )
        session = self._save(BesSession(str(uuid.uuid4()), target, instance))
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if not _same(instance, self.driver.identity(instance.pid)):
                self._error(session, "bes_startup_exited")
            try:
                with self.driver.guard(instance) as valid:
                    if not valid:
                        self._error(session, "bes_instance_identity_changed")
                    window = self._window(session)
                    baseline = self.driver.threads(instance.pid)
                    if baseline:
                        return self._save(
                            replace(session, window_handle=window.handle, baseline_threads=baseline)
                        )
            except BesError as error:
                if error.code not in {"bes_window_unavailable"}:
                    self._error(session, error.code)
            time.sleep(self.poll_interval)
        self._error(session, "bes_startup_timeout_cleanup_required")
        raise AssertionError("unreachable")

    def _limit(
        self,
        target: OwnedProcess,
        percent: int,
        executable: ReviewedBesExecutable | None,
    ) -> BesSession:
        self._owned_target(target)
        matches = [s for s in self.sessions() if s.target.token == target.token]
        if any(
            s.target.pid == target.pid and s.target.token != target.token for s in self.sessions()
        ):
            raise BesError("bes_target_already_assigned")
        session = matches[0] if matches else None
        if session is not None and (
            session.target != target or session.state not in {"idle", "delivered", "resumed"}
        ):
            raise BesError("bes_recovery_required")
        with self.driver.guard(target) as valid:
            if not valid:
                raise BesError("bes_target_identity_changed")
            if session is None or not session.may_have_limited:
                if self.driver.resumed(target) is not True:
                    raise BesError("bes_target_resume_probe_required")
            if session is None:
                session = self._new(target, executable)
            try:
                with self.driver.guard(session.instance) as instance_valid:
                    if not instance_valid:
                        self._error(session, "bes_instance_identity_changed")
                    self._owned_target(target)
                    window = self._window(session)
                    session = self._save(
                        replace(
                            session,
                            state="unknown",
                            may_have_limited=True,
                            reduction_percent=percent,
                            error_code=None,
                        )
                    )
                    handled = self.driver.send_copydata(
                        window,
                        copydata_payload(limit_command(target.pid, percent)),
                        self.command_timeout,
                    )
                    if handled != 1:
                        self._error(session, "bes_command_not_acknowledged_cleanup_required")
                    return self._save(replace(session, state="delivered"))
            except BesError as error:
                self._error(session, error.code)
            except Exception:
                self._error(session, "bes_command_failed_cleanup_required")
        raise AssertionError("unreachable")

    def _target_safe(self, session: BesSession) -> bool:
        if not _same(session.target, self.driver.identity(session.target.pid)):
            # The original target no longer exists. Never probe/control a reused PID.
            return True
        with self.driver.guard(session.target) as valid:
            return valid and self.driver.resumed(session.target) is True

    def _unlimit_target(self, target: OwnedProcess) -> None:
        for session in self.sessions(target.account_id):
            if session.target.token == target.token:
                if session.target != target:
                    raise BesError("bes_target_not_owned")
                self._cleanup(session, exit_instance=False)

    def _stop_account(self, account_id: str | None) -> None:
        errors: list[BesError] = []
        for session in self.sessions(account_id):
            try:
                self._cleanup(session, exit_instance=True)
            except BesError as error:
                errors.append(error)
            except Exception:
                try:
                    self._error(session, "bes_cleanup_required")
                except BesError as error:
                    errors.append(error)
        if errors:
            raise errors[0]

    def _remove(self, session: BesSession) -> None:
        with self._state_lock:
            self.journal.remove(session.token)
            self._unsaved.pop(session.token, None)

    def _cleanup(self, session: BesSession, *, exit_instance: bool) -> None:
        try:
            self._cleanup_checked(session, exit_instance=exit_instance)
        except BesError as error:
            self._error(session, error.code)
        except Exception:
            self._error(session, "bes_cleanup_required")

    def _cleanup_checked(self, session: BesSession, *, exit_instance: bool) -> None:
        with self.driver.guard(session.instance) as valid:
            if not valid:
                if not session.may_have_limited or self._target_safe(session):
                    self._remove(session)
                    return
                self._error(session, "bes_owner_lost_resume_unconfirmed")
            window = self._window(session)
            # Never PID --unlimit: BES 1.7.9 can LIMIT an unknown PID on that command.
            self.driver.send_stop(window, self.command_timeout)
            deadline = time.monotonic() + self.command_timeout
            while True:
                if not _same(session.instance, self.driver.identity(session.instance.pid)):
                    if not session.may_have_limited or self._target_safe(session):
                        self._remove(session)
                        return
                    self._error(session, "bes_owner_lost_resume_unconfirmed")
                workers_gone = bool(session.baseline_threads) and set(
                    self.driver.threads(session.instance.pid)
                ).issubset(set(session.baseline_threads))
                # A never-limited startup failure can close its idle instance safely.
                if not session.may_have_limited or (workers_gone and self._target_safe(session)):
                    break
                if time.monotonic() >= deadline:
                    self._error(session, "bes_resume_unconfirmed_cleanup_required")
                time.sleep(self.poll_interval)
            session = self._save(
                replace(session, state="resumed", may_have_limited=False, error_code=None)
            )
            if not exit_instance:
                return
            window = self._window(session)
            self.driver.send_copydata(window, copydata_payload(exit_now=True), self.command_timeout)
        deadline = time.monotonic() + self.command_timeout
        while _same(session.instance, self.driver.identity(session.instance.pid)):
            if time.monotonic() >= deadline:
                self._error(session, "bes_exit_timeout_cleanup_required")
            time.sleep(self.poll_interval)
        self._remove(session)


class WindowsBesDriver:
    """Win32 transport with read-only thread probes; no process termination method.

    NtQueryInformationThread(35) is dynamically bound and fails closed if unsupported.
    A stand must validate this platform-dependent probe before production use.
    """

    def __init__(self) -> None:
        self._api: Any = None

    def _native(self) -> Any:
        if os.name != "nt":
            raise BesError("bes_requires_windows")
        if self._api is None:
            from ctypes import wintypes as w

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            user = ctypes.WinDLL("user32", use_last_error=True)
            nt = ctypes.WinDLL("ntdll", use_last_error=True)
            declarations: list[tuple[Any, list[Any], Any]] = [
                (kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                (kernel.OpenThread, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                (kernel.CloseHandle, [w.HANDLE], w.BOOL),
                (kernel.GetProcessTimes, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
                (kernel.GetThreadTimes, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
                (
                    kernel.QueryFullProcessImageNameW,
                    [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)],
                    w.BOOL,
                ),
                (kernel.GetProcessIdOfThread, [w.HANDLE], w.DWORD),
                (kernel.WaitForSingleObject, [w.HANDLE, w.DWORD], w.DWORD),
                (
                    nt.NtQueryInformationThread,
                    [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)],
                    w.LONG,
                ),
                (
                    user.SendMessageTimeoutW,
                    [
                        w.HWND,
                        w.UINT,
                        ctypes.c_size_t,
                        ctypes.c_ssize_t,
                        w.UINT,
                        w.UINT,
                        ctypes.POINTER(ctypes.c_size_t),
                    ],
                    ctypes.c_ssize_t,
                ),
            ]
            for function, args, result in declarations:
                function.argtypes, function.restype = args, result
            self._api = (kernel, user, nt, w)
        return self._api

    def identity(self, pid: int) -> ProcessIdentity | None:
        try:
            process = psutil.Process(pid)
            return ProcessIdentity(pid, process.create_time(), _path(process.exe()))
        except psutil.NoSuchProcess:
            return None

    @contextmanager
    def guard(self, process: OwnedProcess) -> Iterator[bool]:
        kernel, _, _, w = self._native()
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, process.pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID.
                yield False
                return
            raise BesError("bes_process_identity_unreadable")
        try:
            times = [w.FILETIME() for _ in range(4)]
            size, buffer = w.DWORD(32768), ctypes.create_unicode_buffer(32768)
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]) or not (
                kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            ):
                raise BesError("bes_process_identity_unreadable")
            created = (
                (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            ) / 10_000_000 - 11644473600
            yield kernel.WaitForSingleObject(handle, 0) == 258 and _same(
                process, ProcessIdentity(process.pid, created, buffer.value)
            )
        finally:
            kernel.CloseHandle(handle)

    def spawn(self, argv: Sequence[str]) -> ProcessIdentity:
        self._native()
        process = psutil.Popen(
            list(argv),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(argv[0]).parent),
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return ProcessIdentity(process.pid, process.create_time(), _path(process.exe()))

    def window(self, handle: int) -> BesWindow | None:
        import win32gui
        import win32process

        if not win32gui.IsWindow(handle):
            return None
        _, pid = win32process.GetWindowThreadProcessId(handle)
        return BesWindow(handle, pid, win32gui.GetClassName(handle))

    def windows(self, pid: int) -> list[BesWindow]:
        result = []
        for info in WindowsWindowLookup().for_pid(pid):
            window = self.window(info.handle)
            if window is not None and window.pid == pid and window.class_name == BES_WINDOW_CLASS:
                result.append(window)
        return result

    def _thread_snapshot(
        self, pid: int, *, counts: bool
    ) -> tuple[tuple[ThreadIdentity, ...], bool]:
        kernel, _, nt, w = self._native()
        before = {thread.id for thread in psutil.Process(pid).threads()}
        identities: list[ThreadIdentity] = []
        zero = bool(before)
        for tid in before:
            handle = kernel.OpenThread(0x0040 | 0x0800, False, tid)
            if not handle:
                raise BesError("bes_thread_probe_unavailable")
            try:
                times = [w.FILETIME() for _ in range(4)]
                if kernel.GetProcessIdOfThread(handle) != pid or not kernel.GetThreadTimes(
                    handle, *[ctypes.byref(t) for t in times]
                ):
                    raise BesError("bes_thread_probe_unavailable")
                identities.append((tid, (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime))
                if counts:
                    count, length = w.ULONG(), w.ULONG()
                    status = nt.NtQueryInformationThread(
                        handle, 35, ctypes.byref(count), ctypes.sizeof(count), ctypes.byref(length)
                    )
                    if status < 0 or length.value != ctypes.sizeof(count):
                        raise BesError("bes_thread_probe_unavailable")
                    zero = zero and count.value == 0
            finally:
                kernel.CloseHandle(handle)
        if before != {thread.id for thread in psutil.Process(pid).threads()}:
            raise BesError("bes_thread_snapshot_changed")
        return tuple(sorted(identities)), zero

    def threads(self, pid: int) -> tuple[ThreadIdentity, ...]:
        return self._thread_snapshot(pid, counts=False)[0]

    def resumed(self, target: OwnedProcess) -> bool | None:
        try:
            with self.guard(target) as valid:
                return valid and self._thread_snapshot(target.pid, counts=True)[1]
        except BesError, psutil.Error, OSError:
            return None

    def _send(
        self, window: BesWindow, message: int, wparam: int, lparam: int, timeout: float
    ) -> int:
        _, user, _, _ = self._native()
        if self.window(window.handle) != window:
            raise BesError("bes_window_ownership_changed")
        result = ctypes.c_size_t()
        if not user.SendMessageTimeoutW(
            window.handle,
            message,
            wparam,
            lparam,
            0x23,
            max(1, math.ceil(timeout * 1000)),
            ctypes.byref(result),
        ):
            raise BesError("bes_message_timeout_or_failed")
        return result.value

    def send_copydata(self, window: BesWindow, payload: bytes, timeout: float) -> int:
        class CopyData(ctypes.Structure):
            _fields_ = [
                ("dwData", ctypes.c_size_t),
                ("cbData", ctypes.c_uint32),
                ("lpData", ctypes.c_void_p),
            ]

        if len(payload) != BES_COPYDATA_SIZE:
            raise ValidationError("bes_invalid_payload")
        buffer = ctypes.create_string_buffer(payload)
        data = CopyData(BES_COPYDATA_ID, len(payload), ctypes.addressof(buffer))
        return self._send(window, 0x004A, 0, ctypes.addressof(data), timeout)

    def send_stop(self, window: BesWindow, timeout: float) -> None:
        self._send(window, 0x0111, BES_STOP_COMMAND, 0, timeout)
