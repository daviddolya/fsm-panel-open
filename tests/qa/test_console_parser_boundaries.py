"""Independent parser checks against the final sanitized console contract."""

import builtins
import ctypes
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, timedelta, timezone
from pathlib import Path

import pytest

from fsm_panel.adapters.console_parser import (
    AccountCacheProposal,
    ConsoleParserContext,
    ConsoleParserError,
    RelayLostProposal,
    parse_console_log_tail,
    read_console_log,
)

T0 = 1_767_225_600  # 2026-01-01 00:00:00 UTC.
PRIVATE = "synthetic_private_identity"
TICKET = "[SteamNetSockets] Received Steam datagram ticket for server"
FRIEND = "CS_App_Lifetime_Gamestats: Recording FriendJoin"
MENU = "CSGO_GAME_UI_STATE_MAINMENU -> CSGO_GAME_UI_STATE_MAINMENU"
LOADING = "CSGO_GAME_UI_STATE_MAINMENU -> CSGO_GAME_UI_STATE_LOADINGSCREEN"
PING = "[SteamNetSockets] Refreshing ping measurements"
RELAY = "SDR RelayNetworkStatus:"


def context(**changes):
    values = {
        "initial_account": PRIVATE,
        "tail_account": PRIVATE,
        "fallback_account": 23,
        "clock": lambda: T0,
        "year": lambda: 2026,
        "local_timestamp": lambda value: value.replace(tzinfo=UTC).timestamp(),
    }
    values.update(changes)
    return ConsoleParserContext(**values)


def ticket(token, date="1/1 0:0:1"):
    return f"{date} {TICKET} {token} vport 0"


def discard(token):
    return f"] Discarding inactive session SDR server {token} vport 0"


def auth(value):
    return f"[SteamNetSockets] AuthStatus (steamid: {value}):"


def parsed(lines, **changes):
    return parse_console_log_tail("\n".join(lines).encode(), context(**changes))


@dataclass
class ReadAudit:
    """Observe real synthetic file I/O without replacing its byte semantics."""

    target: Path
    opened_modes: list[str] = field(default_factory=list)
    reads: list[tuple[int, int]] = field(default_factory=list)
    seeks: list[tuple[int, int]] = field(default_factory=list)
    handles: list[object] = field(default_factory=list)
    after_read: Callable[[], None] | None = None
    read_error: bool = False
    close_error: bool = False
    short_read: bool = False

    def install(self, monkeypatch):
        real_open = Path.open
        audit = self

        class AuditedFile:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                try:
                    return self.handle.__exit__(*args)
                finally:
                    if audit.close_error:
                        raise OSError("private synthetic close failure")

            def seek(self, offset, whence=0):
                audit.seeks.append((offset, whence))
                return self.handle.seek(offset, whence)

            def read(self, size=-1):
                audit.reads.append((self.handle.tell(), size))
                if audit.read_error:
                    raise OSError("private synthetic read failure")
                result = self.handle.read(size)
                if audit.after_read is not None:
                    audit.after_read()
                return result[:-1] if audit.short_read else result

        def opened(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path != audit.target:
                return handle
            audit.opened_modes.append(mode)
            audit.handles.append(handle)
            return AuditedFile(handle)

        monkeypatch.setattr(Path, "open", opened)
        return self


def sparse_log(path, size, tail=b"\n"):
    """Prepare a copied log with a known tail and no original application data."""
    assert size >= len(tail)
    with path.open("wb") as handle:
        handle.seek(size - len(tail))
        handle.write(tail)
    return path


def test_empty_normal_result_keeps_boolean_defaults_and_unused_clock_observation():
    calls = []

    def now():
        calls.append("clock")
        return -(10**9)  # The initial clock-minus-17 cannot filter observations.

    def enabled():
        calls.append("srt_enabled")
        return False

    result = parse_console_log_tail(b"", context(clock=now, srt_enabled=enabled))
    row = result.observation.as_legacy_tuple()
    assert row == (PRIVATE, "", False, False, 0, 0, 0, 0)
    assert tuple(type(value) for value in row) == (str, str, bool, bool, int, int, int, int)
    assert calls == ["clock", "srt_enabled"]
    assert result.identity_origin == "cached" and not result.legacy_fallback
    assert result.complete and not result.proposals
    assert result.identity_verified is False


@pytest.mark.parametrize("first", [None, PRIVATE])
@pytest.mark.parametrize("second", [None, PRIVATE])
def test_identity_and_decimal_tail_budget_use_independent_cache_observations(first, second):
    result = parse_console_log_tail(b"", context(initial_account=first, tail_account=second))
    assert result.observation.acc == (23 if first is None else PRIVATE)
    assert result.identity_origin == ("fallback" if first is None else "cached")
    assert result.read.byte_limit == (12_000_000 if second is None else 2_000_000)
    assert not result.proposals  # Fallback identity never becomes a cache write.
    assert result.identity_verified is False


def test_nondefault_legacy_tuple_preserves_all_eight_distinct_slots():
    result = parsed(
        [
            "1/1 0:0:1 " + FRIEND,
            "1/1 0:0:2 " + PING,
            ticket("server-token", "1/1 0:0:3"),
            "1/1 0:0:4 " + MENU,
            "1/1 0:0:5 " + LOADING,
            "1/1 0:0:6 -> CSGO_GAME_UI_STATE_MAINMENU",
            auth("account-token"),
        ]
    )
    assert result.observation.as_legacy_tuple() == (
        "account-token",
        "server-token",
        T0 + 1,
        T0 + 2,
        T0 + 3,
        T0 + 4,
        T0 + 5,
        T0 + 6,
    )
    assert result.complete and result.identity_verified is False


def test_duplicate_tickets_remove_only_first_equal_occurrence_and_keep_file_order():
    result = parsed(
        [ticket("A", "1/1 0:0:9"), ticket("B", "1/1 0:0:8"), ticket("A", "1/1 0:0:7"), discard("A")]
    )
    assert result.observation.lastmatch == "A"
    assert result.observation.last_match_time == T0 + 7
    result = parsed([ticket("A"), ticket("B", "1/1 0:0:2"), discard("B")])
    assert result.observation.lastmatch == "A"
    assert result.observation.last_match_time == T0 + 2
    result = parsed([ticket("A"), discard("A")])
    assert result.observation.lastmatch == "" and result.observation.last_match_time == T0 + 1


def test_received_ticket_prevents_same_line_discard_and_uses_first_vport():
    line = ticket("A") + " " + discard("A")
    assert parsed([line]).observation.lastmatch == "A"
    result = parsed([ticket("older"), "vport before " + ticket("newer")])
    assert result.observation.lastmatch == ""  # Earlier delimiter produces an appended empty token.
    assert result.observation.last_match_time == T0 + 1


def test_only_ascii_spaces_are_removed_from_tokens_and_auth_identity():
    result = parsed([ticket(" \tA B\r "), auth(" \tC D\r ")])
    assert result.observation.lastmatch == "\tAB\r"
    assert result.observation.acc == "\tCD\r"
    assert result.identity_origin == "auth"
    assert len(result.proposals) == 1
    assert isinstance(result.proposals[0], AccountCacheProposal)
    assert result.proposals[0].account == "\tCD\r" and result.proposals[0].line_number == 2


def test_earlier_auth_terminator_writes_empty_string_instead_of_validating_account():
    result = parsed(["earlier ): " + auth("12345")])
    assert result.observation.acc == "" and result.identity_origin == "auth"
    assert len(result.proposals) == 1 and result.proposals[0].account == ""
    assert result.complete and not result.legacy_fallback and result.identity_verified is False


def test_invalid_ticket_date_preserves_append_but_skips_later_same_line_stages():
    result = parsed([ticket("kept", "13/1 0:0:1") + " " + auth("not-applied"), auth("later")])
    assert result.observation.lastmatch == "kept"
    assert result.observation.last_match_time == 0
    assert result.observation.acc == "later"
    assert result.line_errors == 1 and not result.complete
    assert [(proposal.account, proposal.line_number) for proposal in result.proposals] == [
        ("later", 2)
    ]


def test_missing_ticket_date_is_silent_but_overlapping_menu_has_two_notices():
    result = parsed([ticket("kept", ""), MENU, FRIEND, LOADING, PING])
    row = result.observation.as_legacy_tuple()
    assert row == (PRIVATE, "kept", False, False, 0, 0, 0, 0)
    assert result.missing_timestamps == 5 and result.line_errors == 0


def test_menu_overlap_reads_year_and_converts_twice_in_order():
    events = []
    years = iter((2026, 2027))

    def year():
        value = next(years)
        events.append(("year", value))
        return value

    def convert(value):
        events.append(("convert", value.year))
        return 100.0 if value.year == 2026 else 200.0

    result = parsed(["1/1 0:0:1 " + MENU], year=year, local_timestamp=convert)
    assert events == [("year", 2026), ("convert", 2026), ("year", 2027), ("convert", 2027)]
    assert result.observation.joined_menu_after_match == 100.0
    assert result.observation.last_main_menu_load == 200.0


def test_first_failed_menu_conversion_skips_second_branch_and_next_line_still_runs():
    conversions = []

    def convert(value):
        conversions.append(value.second)
        if value.second == 1:
            raise ValueError("private conversion error")
        return T0 + value.second

    result = parsed(["1/1 0:0:1 " + MENU, "1/1 0:0:2 " + FRIEND], local_timestamp=convert)
    assert conversions == [1, 2]
    assert result.observation.joined_menu_after_match == 0
    assert result.observation.last_main_menu_load == 0
    assert result.observation.joined_party == T0 + 2
    assert result.line_errors == 1 and not result.legacy_fallback


def test_explicit_local_timezone_and_first_date_match_determine_epoch():
    line = "prefix01/02 03:04:05suffix 12/31 23:59:59 " + FRIEND
    utc = parsed([line])
    offset = timezone(timedelta(hours=5))
    local = parsed([line], local_timestamp=lambda value: value.replace(tzinfo=offset).timestamp())
    assert utc.observation.joined_party == 1_767_323_045.0
    assert local.observation.joined_party == 1_767_305_045.0


def test_later_valid_dates_can_move_each_ordinary_field_backwards():
    markers = (FRIEND, PING, MENU, LOADING)
    result = parsed(
        ["1/1 0:0:9 " + marker for marker in markers]
        + ["1/1 0:0:2 " + marker for marker in markers]
    )
    row = result.observation.as_legacy_tuple()
    assert row[2:4] == (T0 + 2, T0 + 2)
    assert row[5:] == (T0 + 2, T0 + 2, T0 + 2)


@pytest.mark.parametrize(
    "status, loses",
    [
        ("anyrelay=OK", False),
        ("anyrelay=attempting", False),
        ("anyrelay=waiting", False),
        ("anyrelay=not attempted", False),
        ("anyrelay=retrying123", False),
        ("anyrelay=unknown", False),
        ("anyrelay=not attempted extra", False),
        ("anyrelay=ok trailing", True),
        ("anyrelay=not  attempted", True),
        ("anyrelay=failed-state", True),
        ("anyrelay=123bad", False),
        ("ANYRELAY=failed", False),
    ],
)
def test_relay_regex_uses_exact_key_ascii_words_and_native_fine_tuple(status, loses):
    result = parsed([f"1/1 0:0:1 {RELAY} {status}"], srt_enabled=lambda: True)
    relays = [proposal for proposal in result.proposals if isinstance(proposal, RelayLostProposal)]
    assert [proposal.timestamp for proposal in relays] == ([T0 + 1] if loses else [])


def test_relay_future_and_previous_boundaries_use_proposed_state_for_later_lines():
    lines = [
        f"1/1 0:{minute}:{second} {RELAY} anyrelay=failed"
        for minute, second in ((0, 10), (0, 10), (0, 9), (1, 0), (1, 1))
    ]
    original = context(srt_enabled=lambda: True)
    result = parse_console_log_tail("\n".join(lines).encode(), original)
    assert [proposal.timestamp for proposal in result.proposals] == [T0 + 10, T0 + 60]
    assert [proposal.line_number for proposal in result.proposals] == [1, 4]
    assert original.srt_lost_at == 0


def test_each_dated_relay_loss_observes_fresh_clock_after_local_conversion():
    events = []
    clocks = iter((T0 - 1000, T0 + 100, T0 + 200))

    def now():
        value = next(clocks)
        events.append(("clock", value))
        return value

    def convert(value):
        stamp = int(value.replace(tzinfo=UTC).timestamp())
        events.append(("convert", stamp))
        return stamp

    result = parsed(
        [
            f"1/1 0:0:1 {RELAY} anyrelay=ok",
            f"no date {RELAY} anyrelay=failed",
            f"1/1 0:2:30 {RELAY} anyrelay=failed",
            f"1/1 0:4:10 {RELAY} anyrelay=failed",
        ],
        clock=now,
        local_timestamp=convert,
        srt_enabled=lambda: True,
    )
    assert events == [
        ("clock", T0 - 1000),
        ("convert", T0 + 150),
        ("clock", T0 + 100),
        ("convert", T0 + 250),
        ("clock", T0 + 200),
    ]
    assert [proposal.timestamp for proposal in result.proposals] == [T0 + 150, T0 + 250]
    assert result.complete


def test_relay_then_auth_proposal_order_survives_a_later_line_failure():
    result = parsed(
        [f"1/1 0:0:1 {RELAY} anyrelay=failed; " + auth(PRIVATE), ticket("kept", "13/1 0:0:1")],
        srt_enabled=lambda: True,
    )
    assert [type(proposal) for proposal in result.proposals] == [
        RelayLostProposal,
        AccountCacheProposal,
    ]
    assert [proposal.line_number for proposal in result.proposals] == [1, 1]
    assert result.observation.acc == PRIVATE and result.observation.lastmatch == "kept"
    assert result.line_errors == 1
    assert PRIVATE not in repr(result) + repr(result.observation) + repr(result.proposals)


def test_invalid_utf8_and_partial_first_line_remain_parseable_with_diagnostics():
    raw = ("partial-prefix " + auth("recognized")).encode().replace(b"Auth", b"Au\xffth")
    result = parse_console_log_tail(raw, context())
    assert result.observation.acc == "recognized" and result.identity_origin == "auth"
    assert result.issues and not result.complete
    assert result.line_errors == 0


def test_srt_enable_is_read_for_trailing_empty_line_and_linecount_is_unused():
    for linecount in (-7, 0, 1, 2, 100):
        calls = []

        def enabled():
            calls.append(True)
            return False

        result = parse_console_log_tail(
            (auth("first") + "\n" + auth("second") + "\n").encode(),
            context(srt_enabled=enabled),
            linecount=linecount,
        )
        assert len(calls) == 3 and result.observation.acc == "second"
        assert len(result.proposals) == 2


def test_byte_parser_has_no_file_network_or_implicit_clock_dependency(monkeypatch):
    ctx = context()
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("implicit external operation")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(time, "time", forbidden)
        result = parse_console_log_tail(ticket("synthetic").encode(), ctx)
    assert not attempted and result.observation.lastmatch == "synthetic"


def test_initial_clock_failure_is_fixed_error_not_a_false_empty_observation():
    def broken():
        raise PermissionError(PRIVATE)

    with pytest.raises(ConsoleParserError) as caught:
        parse_console_log_tail(b"", context(clock=broken))
    assert PRIVATE not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("cached, limit", [(None, 12_000_000), (PRIVATE, 2_000_000)])
def test_bytes_api_accepts_exact_budget_and_rejects_one_extra_byte_before_line_work(cached, limit):
    lines = []

    def enabled():
        lines.append(True)
        return False

    ctx = context(tail_account=cached, srt_enabled=enabled)
    raw = b"x" * limit
    result = parse_console_log_tail(raw, ctx)
    assert result.read.bytes_read == limit and result.complete and lines == [True]
    with pytest.raises(ConsoleParserError, match="^console_tail_limit_or_type$"):
        parse_console_log_tail(raw + b"x", ctx)
    assert lines == [True]


@pytest.mark.parametrize("cached, limit", [(None, 12_000_000), (PRIVATE, 2_000_000)])
def test_file_tail_uses_one_decimal_bounded_binary_read_and_closes_handle(
    tmp_path, monkeypatch, cached, limit
):
    tail = b"\n" + auth("tail-account").encode()
    source = sparse_log(tmp_path / "copied.log", limit + 137, tail)
    audit = ReadAudit(source).install(monkeypatch)
    result = read_console_log(source, context(tail_account=cached))
    assert audit.opened_modes == ["rb"] and audit.seeks == [(137, 0)]
    assert audit.reads == [(137, limit)]
    assert all(handle.closed for handle in audit.handles)
    assert result.read.byte_limit == limit and result.read.bytes_read == limit
    assert result.read.source_size == limit + 137 and result.read.offset == 137
    assert result.observation.acc == "tail-account"
    assert result.issues and not result.complete


def test_missing_file_uses_integer_zero_fallback_and_reports_incomplete(tmp_path):
    result = read_console_log(tmp_path / "missing.log", context())
    row = result.observation.as_legacy_tuple()
    assert row == ("", "", 0, 0, 0, 0, 0, 0)
    assert tuple(type(value) for value in row) == (str, str, int, int, int, int, int, int)
    assert result.legacy_fallback and result.identity_origin == "file_error"
    assert result.issues and not result.complete
    assert result.identity_verified is False


def test_read_error_closes_handle_and_cannot_expose_path_or_error(tmp_path, monkeypatch):
    source = tmp_path / f"{PRIVATE}.log"
    source.write_bytes(auth(PRIVATE).encode())
    audit = ReadAudit(source, read_error=True).install(monkeypatch)
    result = read_console_log(source, context())
    assert result.legacy_fallback and not result.complete
    assert all(handle.closed for handle in audit.handles)
    assert PRIVATE not in repr(result) + repr(result.issues)
    source.rename(source.with_suffix(".closed"))


def test_short_read_with_unchanged_metadata_cannot_produce_valid_observation(tmp_path, monkeypatch):
    source = tmp_path / "copied.log"
    source.write_bytes(auth("must-not-be-proposed").encode())
    before = source.stat()
    audit = ReadAudit(source, short_read=True).install(monkeypatch)
    result = read_console_log(source, context())
    after = source.stat()
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
    assert result.legacy_fallback and "source_changed" in result.issues
    assert not result.proposals and not result.complete
    assert result.read.bytes_read == before.st_size - 1
    assert audit.handles and all(handle.closed for handle in audit.handles)


def test_cancellation_during_read_closes_handle_without_parsing_any_line(tmp_path, monkeypatch):
    source = tmp_path / "copied.log"
    source.write_bytes(auth("must-not-be-proposed").encode())
    cancelled = threading.Event()
    audit = ReadAudit(source, after_read=cancelled.set).install(monkeypatch)
    result = read_console_log(source, context(), cancelled=cancelled)
    assert audit.reads == [(0, 2_000_000)]
    assert audit.handles and all(handle.closed for handle in audit.handles)
    assert result.observation.acc == PRIVATE and not result.proposals
    assert not result.legacy_fallback and not result.complete and "cancelled" in result.issues


def test_close_failure_returns_legacy_fallback_without_discarding_prior_proposals(
    tmp_path, monkeypatch
):
    source = tmp_path / "copied.log"
    source.write_bytes((f"1/1 0:0:1 {RELAY} anyrelay=failed; " + auth(PRIVATE)).encode())
    audit = ReadAudit(source, close_error=True).install(monkeypatch)
    result = read_console_log(source, context(srt_enabled=lambda: True))
    assert all(handle.closed for handle in audit.handles)
    assert result.observation.as_legacy_tuple() == ("", "", 0, 0, 0, 0, 0, 0)
    assert [type(proposal) for proposal in result.proposals] == [
        RelayLostProposal,
        AccountCacheProposal,
    ]
    assert result.legacy_fallback and not result.complete
    assert PRIVATE not in repr(result) + repr(result.proposals)


def test_change_during_read_rejects_observation_before_creating_proposals(tmp_path, monkeypatch):
    source = tmp_path / "copied.log"
    source.write_bytes(auth("never-proposed").encode())
    original_open = Path.open

    def change():
        with original_open(source, "ab") as writer:
            writer.write(b"appended during read")

    audit = ReadAudit(source, after_read=change).install(monkeypatch)
    result = read_console_log(source, context())
    assert result.legacy_fallback and "source_changed" in result.issues
    assert not result.proposals and all(handle.closed for handle in audit.handles)


def test_change_after_parse_returns_fallback_but_retains_completed_proposals(tmp_path):
    source = tmp_path / "copied.log"
    source.write_bytes((auth(PRIVATE) + "\n").encode())
    calls = []

    def enabled():
        calls.append(True)
        if len(calls) == 2:
            with source.open("ab") as writer:
                writer.write(b"changed while parsing trailing line")
        return False

    result = read_console_log(source, context(srt_enabled=enabled))
    assert result.legacy_fallback and "source_changed" in result.issues
    assert len(result.proposals) == 1 and result.proposals[0].account == PRIVATE
    assert result.observation.acc == "" and not result.complete


def test_partial_first_line_in_a_bounded_tail_is_not_discarded(tmp_path):
    source = tmp_path / "copied.log"
    first_line = auth("partial-line-account").encode() + b"\n"
    source.write_bytes(b"prefix-before-cut" + first_line + b"x" * (2_000_000 - len(first_line)))
    result = read_console_log(source, context())
    assert result.read.offset == len(b"prefix-before-cut")
    assert result.observation.acc == "partial-line-account"
    assert "tail_prefix_omitted" in result.issues and not result.complete


def test_baseexception_propagates_while_file_handle_is_still_closed(tmp_path, monkeypatch):
    source = tmp_path / "copied.log"
    source.write_bytes(("1/1 0:0:1 " + FRIEND).encode())
    audit = ReadAudit(source).install(monkeypatch)

    def interrupt(value):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        read_console_log(source, context(local_timestamp=interrupt))
    assert audit.handles and all(handle.closed for handle in audit.handles)


def test_line_budget_retains_prior_proposals_and_marks_incomplete():
    raw = "\n".join(auth(value) for value in ("first", "second", "third")).encode()
    result = parse_console_log_tail(raw, context(), max_lines=2)
    assert result.observation.acc == "second"
    assert [proposal.account for proposal in result.proposals] == ["first", "second"]
    assert "line_limit" in result.issues and not result.complete


def test_cancellation_between_lines_keeps_finished_line_and_stops_before_next():
    cancelled = threading.Event()

    def enabled():
        cancelled.set()
        return False

    result = parse_console_log_tail(
        (auth("first") + "\n" + auth("second")).encode(),
        context(srt_enabled=enabled),
        cancelled=cancelled,
    )
    assert result.observation.acc == "first"
    assert [proposal.account for proposal in result.proposals] == ["first"]
    assert "cancelled" in result.issues and not result.complete


def test_line_errors_are_diagnostics_without_traceback_or_private_output(capsys):
    result = parsed([ticket(PRIVATE, "13/1 0:0:1")])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert result.line_errors == 1 and not result.complete
    assert PRIVATE not in repr(result)


def test_pre_cancelled_file_read_performs_no_metadata_or_open(tmp_path, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise OSError("synthetic interception")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "open", forbidden)
        result = read_console_log(tmp_path / "unread.log", context(), cancelled=cancelled)
    assert not attempts and not result.complete and result.issues


@pytest.mark.parametrize(
    "path",
    [
        r"\\synthetic.invalid\share\copied.log",
        "https://synthetic.invalid/copied.log",
        r"\\.\C:\synthetic.log",
    ],
)
def test_file_locality_is_rejected_before_any_metadata_or_open(path, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise OSError("synthetic interception")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "open", forbidden)
        result = read_console_log(path, context())
    assert not attempts and result.legacy_fallback and not result.complete


@pytest.mark.skipif(os.name != "nt", reason="Windows remote-drive and alternate-stream policy")
@pytest.mark.parametrize("path", [r"Z:\synthetic.log", r"C:\copied.log:stream", "NUL"])
def test_windows_file_syntax_and_remote_drive_never_reach_metadata(path, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise OSError("synthetic interception")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(
            ctypes.windll.kernel32, "GetDriveTypeW", lambda anchor: 4 if anchor == "Z:\\" else 3
        )
        result = read_console_log(path, context())
    assert not attempts and result.legacy_fallback and not result.complete
