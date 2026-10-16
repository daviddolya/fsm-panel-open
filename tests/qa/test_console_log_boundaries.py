"""Independent locality and partial-result checks; no remote OS call is allowed."""

import ctypes
import os
import threading
from pathlib import Path

import pytest

from fsm_panel.adapters.console_logs import discover_console_logs


def _arguments(root, location):
    if location == "flat":
        return root, []
    # This other root must itself be rejected before metadata. Keeping both
    # roots invalid lets every observed filesystem call be an assertion failure.
    return "https://synthetic.invalid/flat", [root]


def _forbid_metadata(patch, attempts):
    def lstat(path, *args, **kwargs):
        attempts.append(("lstat", str(path)))
        raise OSError("synthetic metadata interception")

    def scandir(path):
        attempts.append(("scandir", str(path)))
        raise OSError("synthetic directory interception")

    patch.setattr(Path, "lstat", lstat)
    patch.setattr(os, "scandir", scandir)


@pytest.mark.parametrize("location", ["flat", "sandbox"])
@pytest.mark.parametrize(
    "root",
    [
        r"\\synthetic.invalid\copied\logs",
        "//synthetic.invalid/copied/logs",
        r"\\?\C:\synthetic\logs",
        r"\\.\C:\synthetic\logs",
        "https://synthetic.invalid/copied/logs",
        "file://synthetic.invalid/copied/logs",
        "synthetic\0logs",
    ],
    ids=["unc", "slash-unc", "extended-device", "device", "https", "file-url", "nul-byte"],
)
def test_nonlocal_root_is_rejected_before_any_metadata(root, location, monkeypatch):
    attempts = []
    flat, sandbox = _arguments(root, location)
    with monkeypatch.context() as patch:
        _forbid_metadata(patch, attempts)
        result = discover_console_logs(flat, sandbox, ["synthetic_account"], 10)
    assert attempts == [], "a rejected root reached a filesystem operation"
    assert result.issues == ("root_unavailable_or_unsafe",)
    assert not result.complete and not result.logs and result.entries_visited == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows device and alternate-stream names")
@pytest.mark.parametrize("location", ["flat", "sandbox"])
@pytest.mark.parametrize("root", ["NUL", r"C:\synthetic\CON.txt", r"C:\synthetic\logs:stream"])
def test_windows_reserved_root_is_rejected_before_any_metadata(root, location, monkeypatch):
    attempts = []
    flat, sandbox = _arguments(root, location)
    with monkeypatch.context() as patch:
        _forbid_metadata(patch, attempts)
        result = discover_console_logs(flat, sandbox, ["synthetic_account"], 10)
    assert attempts == [], "a reserved root reached a filesystem operation"
    assert result.issues == ("root_unavailable_or_unsafe",)
    assert not result.complete and not result.logs and result.entries_visited == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows mapped network drive locality")
@pytest.mark.parametrize("location", ["flat", "sandbox"])
def test_mapped_remote_drive_is_rejected_before_any_metadata(location, monkeypatch):
    attempts, drive_queries = [], []
    flat, sandbox = _arguments(r"Z:\synthetic\copied-logs", location)

    def remote_drive(anchor):
        drive_queries.append(anchor)
        return 4  # DRIVE_REMOTE; this stub performs no Windows/network lookup.

    with monkeypatch.context() as patch:
        _forbid_metadata(patch, attempts)
        patch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", remote_drive)
        result = discover_console_logs(flat, sandbox, ["synthetic_account"], 10)
    assert attempts == [], "a mapped remote drive reached a filesystem operation"
    assert drive_queries == ["Z:\\"]
    assert result.issues == ("root_unavailable_or_unsafe",)
    assert not result.complete and not result.logs and result.entries_visited == 0


def _local_roots(tmp_path):
    flat = tmp_path / "flat"
    sandbox = tmp_path / "Counter-Strike Global Offensive" / "game" / "csgo"
    flat.mkdir()
    sandbox.mkdir(parents=True)
    first = flat / "synthetic_account.log"
    first.write_bytes(b"synthetic unread content")
    os.utime(first, (100, 100))
    for name in ("unrelated-one", "unrelated-two"):
        (sandbox / name).write_bytes(b"synthetic unread content")
    return flat, sandbox, first


def test_entry_limit_discards_unfinished_folder_but_retains_completed_root(tmp_path):
    flat, sandbox, first = _local_roots(tmp_path)
    result = discover_console_logs(
        flat, [sandbox], ["synthetic_account"], 10, max_entries=2, clock=lambda: 100
    )
    assert not result.complete and result.issues == ("entry_limit",)
    assert result.entries_visited == 2
    assert [(item.path, item.source) for item in result.logs] == [(first, "flat")]


def test_cancellation_during_directory_iteration_closes_scan_and_retains_prior_root(
    tmp_path, monkeypatch
):
    flat, sandbox, first = _local_roots(tmp_path)
    cancel = threading.Event()
    real_scandir = os.scandir
    closed = []

    class CancelledDirectory:
        def __init__(self, path):
            self.stream = real_scandir(path)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.stream)
            cancel.set()
            return entry

        def __exit__(self, *args):
            try:
                return self.stream.__exit__(*args)
            finally:
                closed.append(True)

    def interrupted_scandir(path):
        return CancelledDirectory(path) if Path(path) == sandbox else real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", interrupted_scandir)
        result = discover_console_logs(
            flat, [sandbox], ["synthetic_account"], 10, cancelled=cancel, clock=lambda: 100
        )
    assert closed == [True]
    assert not result.complete and result.issues == ("cancelled",)
    assert result.entries_visited == 1
    assert [(item.path, item.source) for item in result.logs] == [(first, "flat")]
