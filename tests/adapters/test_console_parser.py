import os
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fsm_panel.adapters.console_parser import (
    AccountCacheProposal,
    ConsoleParserContext,
    ConsoleParserError,
    RelayLostProposal,
    legacy_relay_is_lost,
    parse_console_log_tail,
    read_console_log,
)

TICKET = "[SteamNetSockets] Received Steam datagram ticket for server"
AUTH = "[SteamNetSockets] AuthStatus (steamid:"
MENU = "CSGO_GAME_UI_STATE_MAINMENU -> CSGO_GAME_UI_STATE_MAINMENU"
LOSS = "SDR RelayNetworkStatus: anyrelay=failed;"
STAMP = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC).timestamp()


def context(**changes):
    return replace(
        ConsoleParserContext(
            initial_account="synthetic-cached",
            tail_account="synthetic-cached",
            fallback_account=17,
            clock=lambda: STAMP,
            year=lambda: 2026,
            local_timestamp=lambda value: value.replace(tzinfo=UTC).timestamp(),
        ),
        **changes,
    )


def parse(text, **changes):
    return parse_console_log_tail(text.encode("utf-8"), context(**changes))


def test_empty_normal_tuple_differs_from_file_error_tuple(tmp_path):
    result = parse("")
    assert result.observation.as_legacy_tuple() == (
        "synthetic-cached",
        "",
        False,
        False,
        0,
        0,
        0,
        0,
    )
    assert type(result.observation.joined_party) is bool
    assert result.complete and not result.identity_verified
    failed = read_console_log(tmp_path / "missing.log", context())
    assert failed.observation.as_legacy_tuple() == ("", "", 0, 0, 0, 0, 0, 0)
    assert type(failed.observation.joined_party) is int
    assert failed.legacy_fallback and not failed.complete


@pytest.mark.parametrize(
    "first,second,identity,limit,origin",
    [
        ("known", "known", "known", 2_000_000, "cached"),
        ("known", None, "known", 12_000_000, "cached"),
        (None, "known", 17, 2_000_000, "fallback"),
        (None, None, 17, 12_000_000, "fallback"),
    ],
)
def test_two_cache_observations_are_independent(first, second, identity, limit, origin):
    result = parse("", initial_account=first, tail_account=second)
    assert result.observation.acc == identity and result.identity_origin == origin
    assert result.read.byte_limit == limit
    assert ("fallback_identity" in result.issues) == (origin == "fallback")
    assert not result.identity_verified


def test_fallback_account_validation_is_lazy_and_not_written_to_cache():
    assert not parse("", fallback_account=None).proposals
    with pytest.raises(ConsoleParserError):
        parse("", initial_account=None, fallback_account=None)


def test_duplicate_tickets_remove_only_first_and_keep_timestamp_of_discarded_latest():
    result = parse(
        "\n".join(
            [
                f"2/3 04:05:06 {TICKET} alpha vport 0",
                f"2/3 04:05:07 {TICKET} alpha vport 0",
                f"2/3 04:05:08 {TICKET} beta vport 0",
                "] Discarding inactive session SDR server alpha vport 0",
                "] Discarding inactive session SDR server beta vport 0",
            ]
        )
    )
    assert result.observation.lastmatch == "alpha"
    assert result.observation.last_match_time == STAMP + 2
    assert result.complete


def test_all_discarded_tickets_leave_nonzero_time_and_empty_token():
    result = parse(
        f"2/3 04:05:06 {TICKET} alpha vport 0\n"
        "] Discarding inactive session SDR server alpha vport 0"
    )
    assert result.observation.lastmatch == "" and result.observation.last_match_time == STAMP


def test_invalid_ticket_date_retains_append_skips_same_line_auth_and_continues():
    result = parse(f"13/3 04:05:06 {TICKET} alpha vport 0 {AUTH} first):\n{AUTH} second):")
    assert result.observation.lastmatch == "alpha" and result.observation.last_match_time == 0
    assert result.observation.acc == "second"
    assert result.proposals == (AccountCacheProposal("second", 2),)
    assert result.line_errors == 1 and "line_error" in result.issues


def test_missing_ticket_date_is_silent_and_case_matters():
    result = parse(f"{TICKET} alpha vport 0\n{TICKET.lower()} beta vport 0")
    assert result.observation.lastmatch == "alpha" and not result.missing_timestamps


def test_auth_removes_only_spaces_and_first_delimiter_can_force_empty_identity():
    result = parse(f"{AUTH} a b\t\rc):")
    assert result.observation.acc == "ab\t\rc"
    assert result.proposals == (AccountCacheProposal("ab\t\rc", 1),)
    empty = parse(f"): prefix {AUTH} real):")
    assert empty.observation.acc == "" and empty.proposals == (AccountCacheProposal("", 1),)
    assert empty.identity_origin == "auth" and not empty.identity_verified


def test_independent_timestamp_branches_read_year_and_converter_twice():
    years = iter([2025, 2026])
    observed = []

    def convert(value):
        observed.append(value)
        return value.replace(tzinfo=UTC).timestamp()

    result = parse(f"2/3 04:05:06 {MENU}", year=lambda: next(years), local_timestamp=convert)
    assert [value.year for value in observed] == [2025, 2026]
    assert result.observation.joined_menu_after_match < result.observation.last_main_menu_load
    assert result.observation.last_main_menu_load == STAMP


def test_missing_menu_timestamp_has_two_notices_and_invalid_date_skips_second():
    missing = parse(MENU)
    assert missing.missing_timestamps == 2 and not missing.line_errors
    invalid = parse("13/3 04:05:06 " + MENU)
    assert invalid.line_errors == 1 and not invalid.missing_timestamps


def test_explicit_timezone_and_file_order_not_max_timestamp():
    local = timezone(timedelta(hours=5))
    result = parse(
        "\n".join(
            [
                "2/3 04:05:07 CS_App_Lifetime_Gamestats: Recording FriendJoin",
                "2/3 04:05:06 CS_App_Lifetime_Gamestats: Recording FriendJoin",
            ]
        ),
        local_timestamp=lambda value: value.replace(tzinfo=local).timestamp(),
    )
    assert result.observation.joined_party == STAMP - 5 * 3600


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ok", False),
        ("OK", False),
        ("attempting", False),
        ("waiting", False),
        ("not attempted", False),
        ("retrying", False),
        ("unknown", False),
        ("failed", True),
        ("ok extra", True),
        ("not attempted again", False),
        ("123", False),
        ("", False),
    ],
)
def test_relay_exact_regex_and_initialized_tuple(status, expected):
    assert legacy_relay_is_lost("anyrelay=" + status) is expected
    assert not legacy_relay_is_lost("ANYRELAY=" + status)


def test_same_line_srt_then_auth_proposals_advance_only_copied_state():
    ctx = context(srt_enabled=lambda: True, srt_lost_at=STAMP - 1)
    raw = f"2/3 04:05:06 {LOSS} {AUTH} replacement):\n2/3 04:05:06 {LOSS}".encode()
    result = parse_console_log_tail(raw, ctx)
    assert result.proposals == (RelayLostProposal(STAMP, 1), AccountCacheProposal("replacement", 1))
    assert ctx.srt_lost_at == STAMP - 1 and ctx.initial_account == "synthetic-cached"


def test_srt_future_boundary_is_inclusive_and_previous_boundary_is_strict():
    accepted = parse(
        f"2/3 04:05:06 {LOSS}",
        clock=lambda: STAMP - 60,
        srt_enabled=lambda: True,
        srt_lost_at=STAMP - 1,
    )
    assert accepted.proposals == (RelayLostProposal(STAMP, 1),)
    equal = parse(f"2/3 04:05:06 {LOSS}", srt_enabled=lambda: True, srt_lost_at=STAMP)
    assert not equal.proposals
    future = parse(
        f"2/3 04:05:06 {LOSS}",
        clock=lambda: STAMP - 61,
        srt_enabled=lambda: True,
        srt_lost_at="invalid-unreached",
    )
    assert not future.proposals and not future.line_errors


def test_trailing_empty_line_still_observes_srt_and_linecount_is_unused():
    observed = []

    def enabled():
        observed.append(True)
        return False

    data = f"{AUTH} first):\n{AUTH} second):\n".encode()
    result = parse_console_log_tail(data, context(srt_enabled=enabled), linecount=0)
    assert result.observation.acc == "second" and len(observed) == 3
    assert parse_console_log_tail(data, context(), linecount=-900).observation == result.observation


def test_utf8_ignored_with_diagnostic_and_no_bom_cr_normalization():
    result = parse_console_log_tail(f"\ufeff{AUTH} a\r\t".encode() + b"\xff):", context())
    assert result.observation.acc == "a\r\t" and "invalid_utf8_ignored" in result.issues
    assert not result.complete


def test_line_budget_cancellation_and_byte_limit_are_explicit():
    raw = f"{AUTH} first):\n{AUTH} second):".encode()
    limited = parse_console_log_tail(raw, context(), max_lines=1)
    assert limited.observation.acc == "first" and "line_limit" in limited.issues
    cancel = threading.Event()

    def enabled():
        cancel.set()
        return False

    cancelled = parse_console_log_tail(raw, context(srt_enabled=enabled), cancelled=cancel)
    assert cancelled.observation.acc == "first" and "cancelled" in cancelled.issues
    with pytest.raises(ConsoleParserError, match="console_tail_limit_or_type"):
        parse_console_log_tail(b" " * 2_000_001, context())


def test_initial_provider_failure_is_sanitized_and_does_not_return_file_fallback(tmp_path):
    def fail():
        raise PermissionError("private-source-value")

    with pytest.raises(ConsoleParserError, match="console_initial_observation_failed") as error:
        read_console_log(tmp_path / "missing.log", context(clock=fail))
    assert "private-source-value" not in repr(error.value)


def test_repr_and_diagnostics_do_not_expose_identity_or_log_contents(caplog, capsys):
    result = parse(f"{AUTH} private-identity):\n{TICKET} private-secret")
    assert "private" not in repr(result) + repr(result.proposals) + repr(result.observation)
    assert "private" not in caplog.text + capsys.readouterr().out


@pytest.mark.parametrize("cached,limit", [(True, 2_000_000), (False, 12_000_000)])
def test_file_reads_decimal_tail_and_keeps_partial_first_line(tmp_path, cached, limit):
    path = tmp_path / "synthetic.log"
    marker = f"partial {AUTH} tail-account):".encode()
    with path.open("wb") as handle:
        handle.write(b"ignored-prefix")
        handle.write(marker)
        handle.seek(limit + len(b"ignored-prefix") - 1)
        handle.write(b" ")
    before = path.stat()
    result = read_console_log(path, context(tail_account="yes" if cached else None))
    assert result.observation.acc == "tail-account"
    assert result.read.bytes_read == limit and result.read.offset == len(b"ignored-prefix")
    assert "tail_prefix_omitted" in result.issues and not result.complete
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_file_close_error_retains_prior_proposals_and_returns_fallback(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.log"
    path.write_text(f"{AUTH} proposed):", encoding="utf8")
    original = Path.open
    handles = []

    class FailingClose:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()
            raise OSError("private-close-error")

    def open_copy(selected, *args, **kwargs):
        handle = original(selected, *args, **kwargs)
        handles.append(handle)
        return FailingClose(handle)

    monkeypatch.setattr(Path, "open", open_copy)
    result = read_console_log(path, context())
    assert result.legacy_fallback and result.observation.acc == ""
    assert result.proposals == (AccountCacheProposal("proposed", 1),)
    assert handles and all(handle.closed for handle in handles)


def test_remote_path_rejected_before_any_metadata(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Remote path must be rejected before filesystem metadata")

    monkeypatch.setattr(Path, "lstat", forbidden)
    for path in ("https://invalid/log", r"\\invalid\share\log", "//invalid/share/log"):
        result = read_console_log(path, context())
        assert result.legacy_fallback and result.issues == ("file_unavailable_or_unsafe",)


def test_file_mutation_during_parser_is_detected_without_losing_proposal(tmp_path):
    path = tmp_path / "synthetic.log"
    path.write_text(f"{AUTH} proposed):", encoding="utf8")

    def enabled():
        with path.open("ab") as handle:
            handle.write(b"additional")
        return False

    result = read_console_log(path, context(srt_enabled=enabled))
    assert result.legacy_fallback and "source_changed" in result.issues
    assert result.proposals == (AccountCacheProposal("proposed", 1),)


def test_parent_junction_and_nonregular_files_are_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    path = actual / "synthetic.log"
    path.write_bytes(b"")
    alias = tmp_path / "alias"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(actual), str(alias))
    else:
        alias.symlink_to(actual, target_is_directory=True)
    try:
        assert read_console_log(alias / path.name, context()).legacy_fallback
        assert read_console_log(actual, context()).legacy_fallback
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
