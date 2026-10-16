"""Read-only, bounded discovery of copied console logs; no game orchestration."""

import ctypes
import math
import os
import stat
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal, cast

_GAME_FRAGMENT = r"Counter-Strike Global Offensive\game\csgo"


@dataclass(frozen=True)
class ConsoleLog:
    path: Path = field(repr=False)
    login: str = field(repr=False)
    source: Literal["flat", "sandbox"]
    modified_at: float


@dataclass(frozen=True)
class ConsoleLogDiscovery:
    logs: tuple[ConsoleLog, ...] = field(repr=False)
    issues: tuple[str, ...]
    entries_visited: int

    @property
    def complete(self) -> bool:
        return not self.issues


class ConsoleLogInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_console_log_input")


class _StopScan(Exception):
    pass


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(cast(int | float, value))
    except OverflowError:
        return False


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_root(value: str | Path) -> Path:
    text = os.fspath(value)
    # Reject network/device roots before lstat: merely inspecting a UNC parent
    # can contact a share. Mapped Windows drives need their own locality check.
    if "://" in text or text.startswith(("\\\\", "//")) or "\x00" in text:
        raise ValueError("unsafe_console_log_root")
    path = Path(os.path.abspath(text))
    if os.name == "nt":
        if ":" in str(path)[2:] or os.path.isreserved(path):
            raise ValueError("unsafe_console_log_root")
        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) not in (2, 3, 5, 6):
            raise ValueError("unsafe_console_log_root")
    # lstat every ancestor: resolve() would hide a selected link/junction.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe_console_log_root")
    return path


def discover_console_logs(
    flat_root: str | Path,
    sandbox_roots: Sequence[str | Path],
    launched_logins: Collection[str],
    max_age: float,
    *,
    clock: Callable[[], float] = time.time,
    cancelled: Event | None = None,
    max_entries: int = 20_000,
    max_depth: int = 40,
) -> ConsoleLogDiscovery:
    """Find recent regular files using the two recovered legacy selection rules.

    Roots/logins are explicit snapshots, not live original globals. Every entry
    consumes a shared budget, including directories and irrelevant names. A
    partial result is labelled; it is never a complete account/game observation.
    This does not read contents, enumerate drives, write files or start workers.
    """
    if (
        not isinstance(flat_root, (str, Path))
        or not str(flat_root)
        or isinstance(sandbox_roots, (str, bytes))
        or not isinstance(sandbox_roots, Sequence)
        or len(sandbox_roots) > 32
        or any(not isinstance(root, (str, Path)) or not str(root) for root in sandbox_roots)
        or isinstance(launched_logins, (str, bytes))
        or not isinstance(launched_logins, Collection)
        or len(launched_logins) > 50
        or any(type(login) is not str or not login or len(login) > 256 for login in launched_logins)
        or not _finite(max_age)
        or max_age < 0
        or type(max_entries) is not int
        or not 1 <= max_entries <= 1_000_000
        or type(max_depth) is not int
        or not 0 <= max_depth <= 100
        or not callable(clock)
        or (cancelled is not None and not isinstance(cancelled, Event))
    ):
        raise ConsoleLogInputError()
    logins = frozenset(launched_logins)
    roots = (flat_root, *sandbox_roots)
    logs: list[ConsoleLog] = []
    issues: list[str] = []
    visited = 0

    def issue(code: str) -> None:
        if code not in issues:
            issues.append(code)

    def checkpoint() -> None:
        if cancelled is not None and cancelled.is_set():
            issue("cancelled")
            raise _StopScan

    def entries(folder: Path) -> list[tuple[Path, os.stat_result]]:
        nonlocal visited
        selected = []
        try:
            with os.scandir(folder) as stream:
                for entry in stream:
                    checkpoint()
                    if visited >= max_entries:
                        issue("entry_limit")
                        raise _StopScan
                    visited += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        issue("entry_unavailable")
                        continue
                    if _linked(info):
                        issue("linked_entry_skipped")
                        continue
                    selected.append((Path(entry.path), info))
        except OSError:
            issue("directory_unavailable")
        return selected

    def consider(path: Path, info: os.stat_result, source: Literal["flat", "sandbox"]) -> None:
        checkpoint()
        name = path.name
        matches = name.endswith(".log") if source == "flat" else ".log" in name
        login = name.replace(".log", "")
        if not matches or login not in logins or not stat.S_ISREG(info.st_mode):
            return
        # Refresh metadata immediately before each clock observation, not when
        # listing the directory. Follow no final symlink introduced since then.
        try:
            current = path.lstat()
        except OSError:
            issue("entry_unavailable")
            return
        if _linked(current) or not stat.S_ISREG(current.st_mode):
            issue("entry_changed")
            return
        now = clock()
        if not _finite(now):
            raise ConsoleLogInputError()
        keep = (
            current.st_mtime > now - max_age
            if source == "flat"
            else not now - current.st_mtime > max_age
        )
        if keep:
            logs.append(ConsoleLog(path, login, source, current.st_mtime))

    try:
        for index, value in enumerate(roots):
            checkpoint()
            try:
                root = _safe_root(value)
            except OSError, ValueError:
                issue("root_unavailable_or_unsafe")
                continue
            if index == 0:
                for path, info in entries(root):
                    consider(path, info, "flat")
                continue
            pending = [(root, 0)]
            while pending:
                checkpoint()
                folder, depth = pending.pop()
                # Recheck ancestors before scanning; this is not an atomic
                # filesystem snapshot or protection against hostile races.
                try:
                    _safe_root(folder)
                except OSError, ValueError:
                    issue("root_unavailable_or_unsafe")
                    continue
                in_game = _GAME_FRAGMENT in str(folder).replace("/", "\\")
                children = []
                for path, info in entries(folder):
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= max_depth:
                            issue("depth_limit")
                        else:
                            children.append((path, depth + 1))
                    elif in_game:
                        consider(path, info, "sandbox")
                pending.extend(reversed(children))
    except _StopScan:
        pass
    return ConsoleLogDiscovery(tuple(logs), tuple(issues), visited)
