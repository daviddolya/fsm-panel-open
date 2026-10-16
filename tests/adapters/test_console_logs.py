import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from fsm_panel.adapters.console_logs import (
    ConsoleLogInputError,
    discover_console_logs,
)


def log_file(root, name, timestamp):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text("synthetic unread log contents", encoding="utf8")
    os.utime(path, (timestamp, timestamp))
    return path


def test_flat_and_sandbox_have_different_exact_age_boundaries(tmp_path):
    flat = tmp_path / "flat"
    sandbox = tmp_path / "sandbox"
    game = sandbox / "Counter-Strike Global Offensive" / "game" / "csgo"
    for folder in (flat, game):
        log_file(folder, "boundary.log", 90)
        log_file(folder, "fresh.log", 91)
        log_file(folder, "old.log", 89)
        log_file(folder, "future.log", 101)
    result = discover_console_logs(
        flat, [sandbox], ["boundary", "fresh", "old", "future"], 10, clock=lambda: 100
    )
    assert result.complete
    assert {(item.source, item.login) for item in result.logs} == {
        ("flat", "fresh"),
        ("flat", "future"),
        ("sandbox", "boundary"),
        ("sandbox", "fresh"),
        ("sandbox", "future"),
    }


def test_name_filter_is_case_sensitive_replaces_every_suffix_occurrence_and_does_not_recurse_flat(
    tmp_path,
):
    flat = tmp_path / "flat"
    sandbox = tmp_path / "sandbox"
    game = sandbox / "Counter-Strike Global Offensive" / "game" / "csgo"
    names = ["a.log.log", "b.log.old", "c.LOG", "foreign.log"]
    for folder in (flat, game):
        for name in names:
            log_file(folder, name, 100)
        (folder / "directory.log").mkdir()
    log_file(flat / "nested", "a.log", 100)
    log_file(
        sandbox / "separate" / "counter-strike global offensive" / "game" / "csgo", "a.log", 100
    )
    result = discover_console_logs(
        flat, [sandbox], ["a", "b.old", "c", "directory"], 1, clock=lambda: 100
    )
    assert {(item.source, item.path.name) for item in result.logs} == {
        ("flat", "a.log.log"),
        ("sandbox", "a.log.log"),
        ("sandbox", "b.log.old"),
    }


def test_sandbox_filter_is_substring_not_exact_path_components(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    sandbox = tmp_path / "sandbox"
    game = sandbox / "prefixCounter-Strike Global Offensive" / "game" / "csgo-extra" / "child"
    path = log_file(game, "alice.log", 100)
    result = discover_console_logs(flat, [sandbox], ["alice"], 1, clock=lambda: 100)
    assert [item.path for item in result.logs] == [path]


def test_each_matching_file_reads_clock_afresh_and_log_contents_are_never_opened(
    tmp_path, monkeypatch
):
    for name in ("a.log", "b.log"):
        log_file(tmp_path, name, 100)
    times = iter([100, 102])

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not open log contents")

    monkeypatch.setattr(Path, "open", forbidden)
    result = discover_console_logs(tmp_path, [], ["a", "b"], 1, clock=lambda: next(times))
    assert len(result.logs) == 1
    assert result.complete


def test_missing_root_is_partial_but_does_not_suppress_other_selected_roots(tmp_path):
    game = tmp_path / "Counter-Strike Global Offensive" / "game" / "csgo"
    path = log_file(game, "alice.log", 100)
    result = discover_console_logs(tmp_path / "missing", [game], ["alice"], 1, clock=lambda: 100)
    assert not result.complete
    assert result.issues == ("root_unavailable_or_unsafe",)
    assert [item.path for item in result.logs] == [path]


def test_repeated_explicit_roots_preserve_occurrences_without_global_deduplication(tmp_path):
    game = tmp_path / "Counter-Strike Global Offensive" / "game" / "csgo"
    path = log_file(game, "alice.log", 100)
    result = discover_console_logs(game, [game, game], ["alice"], 1, clock=lambda: 100)
    assert [item.path for item in result.logs] == [path, path, path]
    assert [item.source for item in result.logs] == ["flat", "sandbox", "sandbox"]


def test_entry_budget_counts_irrelevant_names_and_stops_subsequent_roots(tmp_path):
    flat = tmp_path / "flat"
    for name in ("irrelevant", "another", "third"):
        log_file(flat, name, 100)
    game = tmp_path / "Counter-Strike Global Offensive" / "game" / "csgo"
    log_file(game, "alice.log", 100)
    result = discover_console_logs(flat, [game], ["alice"], 1, max_entries=2)
    assert result.entries_visited == 2
    assert result.issues == ("entry_limit",)
    assert not result.logs


def test_depth_budget_reports_unvisited_subtree(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    game = tmp_path / "Counter-Strike Global Offensive" / "game" / "csgo"
    first = log_file(game, "alice.log", 100)
    log_file(game / "child", "alice.log", 100)
    result = discover_console_logs(flat, [game], ["alice"], 1, max_depth=0, clock=lambda: 100)
    assert [item.path for item in result.logs] == [first]
    assert result.issues == ("depth_limit",)


def test_cancellation_retains_prior_results_and_stops_scanning(tmp_path):
    for name in ("a.log", "b.log"):
        log_file(tmp_path, name, 100)
    cancel = threading.Event()

    def now():
        cancel.set()
        return 100

    result = discover_console_logs(tmp_path, [], ["a", "b"], 1, clock=now, cancelled=cancel)
    assert len(result.logs) == 1
    assert result.issues == ("cancelled",)
    result = discover_console_logs(tmp_path, [], ["a", "b"], 1, cancelled=cancel)
    assert result.entries_visited == 0
    assert not result.logs


def test_reparse_ancestor_is_rejected_before_any_enumeration(tmp_path, monkeypatch):
    folder = tmp_path / "synthetic-junction" / "child"
    folder.mkdir(parents=True)
    actual = Path.lstat

    def lstat(path):
        info = actual(path)
        if path == folder.parent:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=1024)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    result = discover_console_logs(folder, [], ["alice"], 1)
    assert not result.complete
    assert result.entries_visited == 0


def test_selected_snapshot_is_not_mutated_by_clock_and_result_repr_has_no_paths_or_login(tmp_path):
    path = log_file(tmp_path, "synthetic_private_account.log", 100)
    logins = ["synthetic_private_account"]

    def now():
        logins.clear()
        return 100

    result = discover_console_logs(tmp_path, [], logins, 1, clock=now)
    assert len(result.logs) == 1
    assert result.logs[0].path == path
    assert "synthetic_private_account" not in repr(result.logs[0])
    assert str(tmp_path) not in repr(result.logs[0])
    assert "synthetic_private_account" not in repr(result)


@pytest.mark.parametrize("window", [-1, float("nan"), float("inf"), True, "10"])
def test_invalid_age_rejected(tmp_path, window):
    with pytest.raises(ConsoleLogInputError, match="^invalid_console_log_input$"):
        discover_console_logs(tmp_path, [], [], window)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_entries": 0},
        {"max_entries": True},
        {"max_depth": -1},
        {"max_depth": 101},
    ],
)
def test_invalid_limits_rejected(tmp_path, kwargs):
    with pytest.raises(ConsoleLogInputError):
        discover_console_logs(tmp_path, [], [], 10, **kwargs)


@pytest.mark.parametrize("logins", ["alice", [""], [7], list(map(str, range(51)))])
def test_invalid_account_snapshot_rejected(tmp_path, logins):
    with pytest.raises(ConsoleLogInputError):
        discover_console_logs(tmp_path, [], logins, 10)


def test_invalid_clock_is_not_a_successful_empty_observation(tmp_path):
    log_file(tmp_path, "alice.log", 100)
    with pytest.raises(ConsoleLogInputError):
        discover_console_logs(tmp_path, [], ["alice"], 10, clock=lambda: float("nan"))


def test_frozen_result_is_explicitly_partial_when_issues_present(tmp_path):
    result = discover_console_logs(tmp_path, [], [], 10)
    assert result.complete
    assert not replace(result, issues=("directory_unavailable",)).complete
