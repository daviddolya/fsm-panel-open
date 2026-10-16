"""Read-only console-copy parser; static observations never authorize game input.

Native grammar is preserved. Time providers, copied state, diagnostics, locality
checks and work limits are explicit reconstruction choices. Nothing applies the
returned cache/relay proposals, prints log data, or uses live account/game state.
"""

import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Literal, cast

from .console_logs import _linked, _local_path, _safe_root

SMALL_TAIL_BYTES = 2_000_000
LARGE_TAIL_BYTES = 12_000_000
RELAY_FINE = ("ok", "attempting", "waiting", "not attempted", "retrying", "unknown")
_DATE = re.compile(r"\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}")
_RELAY = re.compile(r"anyrelay=([A-Za-z]+(?: [A-Za-z]+)?)")
_TICKET = "[SteamNetSockets] Received Steam datagram ticket for server"
_AUTH = "[SteamNetSockets] AuthStatus (steamid"
Number = int | float
IdentityOrigin = Literal["cached", "fallback", "auth", "file_error"]


def _disabled() -> bool:
    return False


class ConsoleParserError(ValueError):
    def __init__(self, code: str = "invalid_console_parser_input") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class ConsoleParserContext:
    initial_account: str | int | None
    tail_account: str | int | None
    fallback_account: int
    clock: Callable[[], Number]
    year: Callable[[], int]
    local_timestamp: Callable[[datetime], Number]
    srt_enabled: Callable[[], bool] = _disabled
    srt_lost_at: Number = 0
    relay_fine: tuple[str, ...] = RELAY_FINE


@dataclass(frozen=True)
class ConsoleObservation:
    acc: str | int = field(repr=False)
    lastmatch: str = field(default="", repr=False)
    joined_party: bool | Number = field(default=False, repr=False)
    pinging_server: bool | Number = field(default=False, repr=False)
    last_match_time: Number = field(default=0, repr=False)
    joined_menu_after_match: Number = field(default=0, repr=False)
    last_match_loading: Number = field(default=0, repr=False)
    last_main_menu_load: Number = field(default=0, repr=False)

    def as_legacy_tuple(self) -> tuple[str | Number, ...]:
        """Sensitive compatibility values; not an account-validation result."""
        return (
            self.acc,
            self.lastmatch,
            self.joined_party,
            self.pinging_server,
            self.last_match_time,
            self.joined_menu_after_match,
            self.last_match_loading,
            self.last_main_menu_load,
        )


@dataclass(frozen=True)
class RelayLostProposal:
    timestamp: float = field(repr=False)
    line_number: int


@dataclass(frozen=True)
class AccountCacheProposal:
    account: str = field(repr=False)
    line_number: int


@dataclass(frozen=True)
class ConsoleReadInfo:
    byte_limit: int
    bytes_read: int
    source_size: int
    offset: int


@dataclass(frozen=True)
class ConsoleParseResult:
    observation: ConsoleObservation = field(repr=False)
    proposals: tuple[RelayLostProposal | AccountCacheProposal, ...] = field(repr=False)
    identity_origin: IdentityOrigin
    read: ConsoleReadInfo
    issues: tuple[str, ...] = ()
    line_errors: int = 0
    missing_timestamps: int = 0
    legacy_fallback: bool = False

    @property
    def complete(self) -> bool:
        """All selected input was processed without diagnostics; never live validity."""
        return not self.issues and not self.legacy_fallback

    @property
    def identity_verified(self) -> bool:
        """Neither a cache string, log marker nor fallback authenticates an account."""
        return False


@dataclass
class _State:
    context: ConsoleParserContext = field(repr=False)
    acc: str | int = field(repr=False)
    identity_origin: IdentityOrigin
    byte_limit: int
    srt_lost_at: Number = field(repr=False)
    servers: list[str] = field(default_factory=list, repr=False)
    values: dict[str, Number] = field(
        default_factory=lambda: {
            "joined_party": False,
            "pinging_server": False,
            "last_match_time": 0,
            "joined_menu_after_match": 0,
            "last_match_loading": 0,
            "last_main_menu_load": 0,
        },
        repr=False,
    )
    proposals: list[RelayLostProposal | AccountCacheProposal] = field(
        default_factory=list, repr=False
    )
    issues: list[str] = field(default_factory=list)
    line_errors: int = 0
    missing_timestamps: int = 0

    def issue(self, code: str) -> None:
        if code not in self.issues:
            self.issues.append(code)


def _number(value: object) -> Number:
    if type(value) not in (int, float):
        raise ConsoleParserError()
    try:
        if not math.isfinite(cast(Number, value)):
            raise ConsoleParserError()
    except OverflowError:
        raise ConsoleParserError() from None
    return cast(Number, value)


def _account(value: object) -> None:
    if value is None:
        return
    if type(value) is str and len(value) <= 4096:
        return
    if type(value) is int and value.bit_length() <= 128:
        return
    raise ConsoleParserError()


def _prepare(context: ConsoleParserContext, linecount: int, max_lines: int, cancelled) -> _State:
    if (
        type(context) is not ConsoleParserContext
        or type(linecount) is not int
        or type(max_lines) is not int
        or not 1 <= max_lines <= 1_000_000
        or (cancelled is not None and not isinstance(cancelled, Event))
        or not all(
            callable(provider)
            for provider in (
                context.clock,
                context.year,
                context.local_timestamp,
                context.srt_enabled,
            )
        )
    ):
        raise ConsoleParserError()
    _account(context.initial_account)
    acc = context.initial_account
    origin: IdentityOrigin = "cached"
    if not acc:
        if type(context.fallback_account) is not int or not 1 <= context.fallback_account <= 99999:
            raise ConsoleParserError()
        acc, origin = context.fallback_account, "fallback"
    try:
        # This observation is unused by the source but can fail before file work.
        _number(_number(context.clock()) - 17)
    except Exception:
        raise ConsoleParserError("console_initial_observation_failed") from None
    _account(context.tail_account)
    return _State(
        context,
        cast(str | int, acc),
        origin,
        SMALL_TAIL_BYTES if context.tail_account else LARGE_TAIL_BYTES,
        context.srt_lost_at,
    )


def legacy_relay_is_lost(line: str, fine: tuple[str, ...] = RELAY_FINE) -> bool:
    """Recovered regex/membership with an explicit immutable fine-status snapshot."""
    if type(line) is not str:
        raise ConsoleParserError()
    match = _RELAY.search(line)
    if match is None:
        return False
    if type(fine) is not tuple or len(fine) > 128 or any(type(s) is not str for s in fine):
        raise ConsoleParserError()
    return match.group(1).lower() not in fine


def _timestamp(state: _State, line: str, *, notice: bool = False) -> float | None:
    found = _DATE.search(line)
    if found is None:
        if notice:
            state.missing_timestamps += 1
            state.issue("missing_timestamp")
        return None
    year = state.context.year()
    if type(year) is not int or not 1 <= year <= 9999:
        raise ConsoleParserError()
    date = datetime.strptime(str(year) + "/" + found.group(0), "%Y/%m/%d %H:%M:%S")
    return float(_number(state.context.local_timestamp(date)))


def _line(state: _State, line: str, number: int) -> None:
    if _TICKET in line:
        token = line[line.index(_TICKET) + 59 : line.index("vport")].replace(" ", "")
        state.servers.append(token)
        stamp = _timestamp(state, line)
        if stamp is not None:
            state.values["last_match_time"] = stamp
    elif "vport 0" in line and "] Discarding inactive session" in line:
        token = line[line.index("SDR server") + 10 : line.index("vport")].replace(" ", "")
        if token in state.servers:
            state.servers.remove(token)
    for marker, name in (
        ("CS_App_Lifetime_Gamestats: Recording FriendJoin", "joined_party"),
        ("CSGO_GAME_UI_STATE_MAINMENU -> CSGO_GAME_UI_STATE_MAINMENU", "joined_menu_after_match"),
        ("CSGO_GAME_UI_STATE_MAINMENU -> CSGO_GAME_UI_STATE_LOADINGSCREEN", "last_match_loading"),
        ("-> CSGO_GAME_UI_STATE_MAINMENU", "last_main_menu_load"),
        ("[SteamNetSockets] Refreshing ping measurements", "pinging_server"),
    ):
        if marker in line:
            stamp = _timestamp(state, line, notice=True)
            if stamp is not None:
                state.values[name] = stamp
    enabled = state.context.srt_enabled()
    if type(enabled) is not bool:
        raise ConsoleParserError()
    if (
        enabled
        and "SDR RelayNetworkStatus:" in line
        and legacy_relay_is_lost(line, state.context.relay_fine)
    ):
        stamp = _timestamp(state, line)
        if stamp is not None:
            latest = _number(_number(state.context.clock()) + 60)
            if latest >= stamp and stamp > _number(state.srt_lost_at):
                state.srt_lost_at = stamp
                state.proposals.append(RelayLostProposal(stamp, number))
    if _AUTH in line:
        start, end = line.index("AuthStatus (steamid:"), line.index("):")
        state.acc = line[start + 20 : end].replace(" ", "")
        state.identity_origin = "auth"
        state.proposals.append(AccountCacheProposal(state.acc, number))


def _fallback(state: _State, code: str, read: ConsoleReadInfo | None = None) -> ConsoleParseResult:
    state.issue(code)
    return ConsoleParseResult(
        ConsoleObservation("", "", 0, 0, 0, 0, 0, 0),
        tuple(state.proposals),
        "file_error",
        read or ConsoleReadInfo(state.byte_limit, 0, 0, 0),
        tuple(state.issues),
        state.line_errors,
        state.missing_timestamps,
        True,
    )


def _parse(raw: bytes, state: _State, read: ConsoleReadInfo, cancelled, max_lines: int):
    if read.offset:
        state.issue("tail_prefix_omitted")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="ignore")
        state.issue("invalid_utf8_ignored")
    # Stream exactly split('\n') semantics, including the trailing empty line,
    # without allocating a list of millions of empty strings in a newline flood.
    position = 0
    number = 0
    while True:
        if cancelled is not None and cancelled.is_set():
            state.issue("cancelled")
            break
        if number >= max_lines:
            state.issue("line_limit")
            break
        end = text.find("\n", position)
        line = text[position:] if end == -1 else text[position:end]
        number += 1
        try:
            _line(state, line, number)
        except Exception:
            # Preserve preceding mutations and continue; never print private data.
            state.line_errors += 1
            state.issue("line_error")
        if end == -1:
            break
        position = end + 1
    if state.identity_origin == "fallback":
        state.issue("fallback_identity")
    return ConsoleParseResult(
        ConsoleObservation(state.acc, state.servers[-1] if state.servers else "", **state.values),
        tuple(state.proposals),
        state.identity_origin,
        read,
        tuple(state.issues),
        state.line_errors,
        state.missing_timestamps,
    )


def parse_console_log_tail(
    raw: bytes,
    context: ConsoleParserContext,
    *,
    linecount: int = 1,
    cancelled: Event | None = None,
    max_lines: int = 200_000,
) -> ConsoleParseResult:
    """Parse caller-supplied tail bytes. The recovered linecount is unused.

    Clock/year/converter callbacks supply observations only. They are trusted
    dependencies; this utility never applies cache/relay proposals or uses GSI.
    """
    state = _prepare(context, linecount, max_lines, cancelled)
    if type(raw) is not bytes or len(raw) > state.byte_limit:
        raise ConsoleParserError("console_tail_limit_or_type")
    return _parse(
        raw, state, ConsoleReadInfo(state.byte_limit, len(raw), len(raw), 0), cancelled, max_lines
    )


def _snapshot(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _parents(path: Path) -> tuple[tuple[int, int], ...]:
    _safe_root(path.parent)
    return tuple((s.st_dev, s.st_ino) for part in path.parents for s in (part.lstat(),))


def read_console_log(
    path: str | Path,
    context: ConsoleParserContext,
    *,
    linecount: int = 1,
    cancelled: Event | None = None,
    max_lines: int = 200_000,
) -> ConsoleParseResult:
    """Read a bounded tail from an explicit local regular-file copy, then parse.

    No original files are changed. Parsing happens while the handle is open;
    a later close failure retains the ordered proposals beside fallback values.
    Concurrent changes reject the observation.
    These checks are not an atomic defense against hostile directory replacement.
    """
    state = _prepare(context, linecount, max_lines, cancelled)
    if cancelled is not None and cancelled.is_set():
        return _fallback(state, "cancelled")
    read = None
    try:
        if not isinstance(path, (str, Path)) or not str(path):
            raise ValueError()
        # Check the original spelling before Path normalizes URI separators.
        selected = _local_path(path)
        parents = _parents(selected)
        before = selected.lstat()
        if _linked(before) or not stat.S_ISREG(before.st_mode):
            return _fallback(state, "file_unavailable_or_unsafe")
        with selected.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _snapshot(opened) != _snapshot(before):
                return _fallback(state, "source_changed")
            offset = max(opened.st_size - state.byte_limit, 0)
            handle.seek(offset, 0)
            raw = handle.read(state.byte_limit)
            after = os.fstat(handle.fileno())
            read = ConsoleReadInfo(state.byte_limit, len(raw), opened.st_size, offset)
            if _snapshot(before) != _snapshot(after) or len(raw) != opened.st_size - offset:
                return _fallback(state, "source_changed", read)
            result = _parse(raw, state, read, cancelled, max_lines)
        current = selected.lstat()
        if (
            _linked(current)
            or _snapshot(before) != _snapshot(current)
            or parents != _parents(selected)
        ):
            return _fallback(state, "source_changed", read)
    except Exception:
        return _fallback(state, "file_unavailable_or_unsafe", read)
    return result
