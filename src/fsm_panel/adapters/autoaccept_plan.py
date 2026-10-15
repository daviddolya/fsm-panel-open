"""Resumable, inert traces of recovered autoaccept operations.

Nothing here reads a clock, setting, account file, window or pixel, acquires a
lock, sleeps, sends input, logs, or contacts co-op. A driver supplies observations
and acknowledges described effects. See docs/autoaccept-plan.md for boundaries.
"""

import math
from collections import Counter
from collections.abc import Generator
from dataclasses import dataclass
from typing import cast


class AutoacceptPlanError(ValueError):
    def __init__(self) -> None:
        super().__init__("autoaccept_plan_response_invalid")


@dataclass(frozen=True)
class WindowRef:
    """An enumerated handle/title pair; this does not establish ownership."""

    handle: int
    title: str


@dataclass(frozen=True)
class PixelWindow:
    """Inert identity for a supplied window object; properties are read later."""

    token: str


type Number = int | float
type Argument = str | Number | bool | None | PixelWindow | tuple[WindowRef, ...] | tuple[str, ...]


@dataclass(frozen=True)
class TraceRequest:
    operation: str
    arguments: tuple[Argument, ...] = ()
    # Describes the original operation's own global RLock nesting, not a lock
    # acquired by this module. Standalone child traces exclude caller-held locks.
    lock_depth: int = 0


type Trace = Generator[TraceRequest, object]
type DecisionTrace = Generator[TraceRequest, object, bool]


def _ack(value: object) -> None:
    if value is not None:
        raise AutoacceptPlanError()


def _number(value: object) -> Number:
    if type(value) not in (int, float):
        raise AutoacceptPlanError()
    if type(value) is float and not math.isfinite(value):
        raise AutoacceptPlanError()
    return cast(Number, value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise AutoacceptPlanError()
    return cast(int, value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise AutoacceptPlanError()
    return cast(bool, value)


def _string(value: object) -> str:
    if type(value) is not str:
        raise AutoacceptPlanError()
    return cast(str, value)


def _tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise AutoacceptPlanError()
    return cast(tuple[object, ...], value)


def _pair(value: object) -> tuple[int, int]:
    pair = _tuple(value)
    if len(pair) != 2:
        raise AutoacceptPlanError()
    return _integer(pair[0]), _integer(pair[1])


def _windows(value: object) -> tuple[WindowRef, ...]:
    windows = _tuple(value)
    for item in windows:
        if type(item) is not WindowRef:
            raise AutoacceptPlanError()
        window = cast(WindowRef, item)
        _integer(window.handle)
        _string(window.title)
    return cast(tuple[WindowRef, ...], windows)


def _servers(value: object) -> frozenset[str]:
    if type(value) is not frozenset:
        raise AutoacceptPlanError()
    servers = cast(frozenset[object], value)
    for server in servers:
        _string(server)
    return cast(frozenset[str], servers)


def _effect(operation: str, *arguments: Argument, depth: int = 0) -> Trace:
    _ack((yield TraceRequest(operation, arguments, depth)))


def iter_legacy_dispatch(first_delay: Number, server: str | None) -> Trace:
    """Describe the complete immediate dispatcher, leaving child calls opaque.

    Respond to read_time with a finite number and read_setting/check_* with an
    exact bool. Other requests require None. check_* and batch/pixel requests
    can be expanded using the corresponding generators below. Throws at the
    pixel-operation request model the original ordinary-exception catch; all
    other errors propagate. Invalid protocol values always propagate.
    """
    delay = _number(first_delay)
    if server is not None:
        _string(server)
    yield from _effect("print_accept")
    started = _number((yield TraceRequest("read_time", ("dispatch_start",))))
    alternative = _boolean((yield TraceRequest("read_setting", ("ALTERNATIVE_AUTOACCEPTER",), 1)))
    if alternative:
        yield from _effect("block_input", True, depth=2)
        try:
            yield from _effect("foreground_pixel_operation", depth=2)
        except AutoacceptPlanError:
            raise
        except Exception:
            yield from _effect("print_traceback", depth=2)
        yield from _effect("block_input", False, depth=2)
        return
    if delay > 0:
        yield from _effect("sleep", delay, depth=1)
    for batch in range(4):
        if batch:
            yield from _effect("sleep", 4, depth=1)
        if batch >= 2 and _boolean((yield TraceRequest("check_row_count", (started,), 1))):
            return
        if _boolean((yield TraceRequest("check_abandonment", (server,), 1))):
            return
        yield from _effect("background_click_batch", depth=2)


def iter_legacy_row_count(started: Number) -> DecisionTrace:
    """Count every row with row[6] > started, then apply >=6 or 2v2 >=2.

    read_account_rows accepts a tuple of tuples. Only column six is interpreted;
    it must be a finite builtin number. Missing/invalid columns are protocol
    errors. The original allowed arbitrary Python objects and propagated errors.
    """
    threshold = _number(started)
    rows = _tuple((yield TraceRequest("read_account_rows")))
    count = 0
    for row in rows:
        columns = _tuple(row)
        if len(columns) <= 6:
            raise AutoacceptPlanError()
        if _number(columns[6]) > threshold:
            count += 1
    if count >= 6:
        return True
    running_2v2 = _boolean((yield TraceRequest("read_setting", ("RUNNING2VS2",))))
    return running_2v2 and count >= 2


def iter_legacy_abandonment(server: str | None) -> DecisionTrace:
    """Describe singleton-set equality, optional leave, short IDs and logging.

    A None server performs no read. read_coop_present is the supplied result of
    COOP is not None, not the object's truthiness. read_coop_servers requires a
    frozenset of exact strings, and is requested again for the log after leaving.
    Effects are acknowledged only; no co-op client is imported or invoked.
    """
    if server is None:
        return False
    _string(server)
    servers = _servers((yield TraceRequest("read_coop_servers")))
    if servers == frozenset((server,)):
        return False
    if _boolean((yield TraceRequest("read_coop_present"))):
        yield from _effect("leave_match", "match_cancelled")
    short_server = _string((yield TraceRequest("short_id", (server,))))
    current = _servers((yield TraceRequest("read_coop_servers")))
    short_current = []
    for item in current:
        short_current.append(_string((yield TraceRequest("short_id", (item,)))))
    yield from _effect("log_accept_abandoned", short_server, tuple(sorted(short_current)))
    return True


def iter_legacy_background_batch() -> Trace:
    """Describe the full batch, including the source-level None button-up arg.

    enumerate_title_windows returns WindowRef tuples; shuffle_windows returns a
    permutation of that same tuple, including duplicates. make_long returns an
    exact integer supplied by the driver; no pywin32 conversion is guessed.
    Ordinary effect failures inside the batch skip its remainder, then describe
    unblocking. BaseException and protocol failures propagate without unblocking.
    """
    yield from _effect("block_input", True)
    try:
        width, _height = _pair((yield TraceRequest("screen_size")))
        _x, y = _pair((yield TraceRequest("cursor_position")))
        yield from _effect("move_cursor", width - 1, y)
        windows = _windows((yield TraceRequest("enumerate_title_windows", ("Counter-Strike 2",))))
        shuffled = _windows((yield TraceRequest("shuffle_windows", (windows,))))
        if Counter(shuffled) != Counter(windows):
            raise AutoacceptPlanError()
        yield from _effect("sleep", 0.15)
        for window in shuffled:
            yield from _effect("print_window", window.handle, window.title)
            running_2v2 = _boolean((yield TraceRequest("read_setting", ("RUNNING2VS2",))))
            xy = (201, 113) if running_2v2 else (175, 115)
            packed = _integer((yield TraceRequest("make_long", xy)))
            yield from _effect("post_message", window.handle, "WM_MOUSEMOVE", 0, packed)
            packed = _integer((yield TraceRequest("make_long", (0, 0))))
            yield from _effect(
                "post_message", window.handle, "WM_LBUTTONDOWN", "VK_LBUTTON", packed
            )
            packed = _integer((yield TraceRequest("make_long", (0, 0))))
            yield from _effect("post_message", window.handle, "WM_LBUTTONUP", None, packed)
            yield from _effect("sleep", 0.1)
    except AutoacceptPlanError:
        raise
    except Exception:
        pass
    yield from _effect("block_input", False)


def iter_legacy_title_activation(title: str) -> Trace:
    """Describe three foreground-then-restore attempts under global_lock."""
    _string(title)
    for _ in range(3):
        try:
            handle = _integer((yield TraceRequest("find_window", (None, title), 1)))
            yield from _effect("set_foreground_window", handle, depth=1)
            yield from _effect("show_window", handle, "SW_RESTORE", depth=1)
            return
        except AutoacceptPlanError:
            raise
        except Exception:
            yield from _effect("sleep", 0.2, depth=1)


def iter_legacy_foreground_pixels() -> Trace:
    """Describe first-window-only polling and all subsequent clicks.

    read_launched_accounts returns account-name strings in observed order, then
    each title is requested when its account is reached. Window objects are
    represented by PixelWindow tokens; their title and four integer properties
    are read when reached, after activation where appropriate. Pixel responses
    are exactly three integers. No timeout or input is actually performed.
    """
    accounts = _tuple((yield TraceRequest("read_launched_accounts")))
    for account in accounts:
        _string(account)
    polled = False
    for account in accounts:
        title = _string((yield TraceRequest("read_account_window_title", (cast(str, account),))))
        windows = _tuple((yield TraceRequest("get_windows_with_title", (title,))))
        for item in windows:
            if type(item) is not PixelWindow:
                raise AutoacceptPlanError()
            window = cast(PixelWindow, item)
            _string(window.token)
            window_title = _string((yield TraceRequest("read_window_title", (window,))))
            yield from _effect("activate_title", window_title)
            yield from _effect("sleep", 0.15)
            properties = []
            for name in ("left", "top", "width", "height"):
                properties.append(
                    _integer((yield TraceRequest("read_window_property", (window, name))))
                )
            x, y, _width, height = properties
            if not polled:
                started = _number((yield TraceRequest("read_time", ("pixel_start",))))
                polled = True
                while _number((yield TraceRequest("read_time", ("pixel_elapsed",)))) - started < 7:
                    yield from _effect("sleep", 0.1)
                    cs_height = _integer((yield TraceRequest("read_setting", ("CS_HEIGHT",))))
                    pixel = _tuple(
                        (yield TraceRequest("pixel", (x + 158, y + 113 + height - cs_height)))
                    )
                    if len(pixel) != 3:
                        raise AutoacceptPlanError()
                    channels = tuple(_integer(channel) for channel in pixel)
                    if channels[1] > 88:
                        now = _number((yield TraceRequest("read_time", ("pixel_found",))))
                        yield from _effect("print_found_accept", now - started)
                        yield from _effect("sleep", 2)
                        break
            running_2v2 = _boolean((yield TraceRequest("read_setting", ("RUNNING2VS2",))))
            cs_height = _integer((yield TraceRequest("read_setting", ("CS_HEIGHT",))))
            yield from _effect(
                "move_cursor", x + (201 if running_2v2 else 158), y + 113 + height - cs_height
            )
            yield from _effect("sleep", 0.05)
            yield from _effect("mouse_down")
            yield from _effect("sleep", 0.14)
            yield from _effect("mouse_up")
