"""Read-only top-level window lookup abstraction. No clicks, injection or game actions."""

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from fsm_panel.core.models import UnavailableError


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    pid: int
    title: str = field(repr=False)
    visible: bool = True


class WindowLookup(Protocol):
    def for_pid(self, pid: int) -> list[WindowInfo]: ...


class FakeWindowLookup:
    def __init__(self, windows: Iterable[WindowInfo] = ()) -> None:
        self.windows = list(windows)

    def for_pid(self, pid: int) -> list[WindowInfo]:
        return [window for window in self.windows if window.pid == pid]


class WindowsWindowLookup:
    def for_pid(self, pid: int) -> list[WindowInfo]:
        if os.name != "nt":
            raise UnavailableError("window_lookup_requires_windows")
        try:
            import win32gui
            import win32process

            result: list[WindowInfo] = []

            def collect(handle: int, _: object) -> None:
                try:
                    _, owner = win32process.GetWindowThreadProcessId(handle)
                    if owner == pid:
                        result.append(WindowInfo(handle, owner, win32gui.GetWindowText(handle),
                                                 bool(win32gui.IsWindowVisible(handle))))
                except Exception:
                    # Window may close between enumeration and lookup.
                    return

            win32gui.EnumWindows(collect, None)
            return result
        except Exception:
            raise UnavailableError("window_lookup_failed") from None
