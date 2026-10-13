"""Independent checks of the bounded video contract using synthetic local files."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from fsm_panel import maintenance
from fsm_panel.adapters import video
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui import video as video_ui
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.video import VideoPanel
from fsm_panel.ui.window import MainWindow


def synthetic_files(directory, *, equal=False):
    template, source = directory / "template copy.txt", directory / "source config.txt"
    template.write_bytes(b'"VendorID" "10"\r\n"DeviceID" "20"\r\n// keep 10 and 20\r\n')
    source.write_bytes(template.read_bytes() if equal else b'"VendorID" "30"\n"DeviceID" "40"\n')
    return template, source


def bytes_in(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def cli_arguments(template, source):
    return ["video", "--template", str(template), "--steam-config", str(source)]


@pytest.mark.parametrize(
    "old,new,extra,expected_ids,expected_extra",
    [
        (("1", "2"), ("2", "1"), '"key12" "12"', ("1", "1"), '"key11" "11"'),
        (("1", "1"), ("2", "1"), "// 1 11", ("2", "2"), "// 2 22"),
        (("12", "2"), ("12", "7"), "// 12 2", ("17", "7"), "// 17 7"),
        (("01", "02"), ("1", "2"), "// 0102", ("1", "2"), "// 12"),
    ],
)
def test_recovered_global_replacement_collisions_are_explicit(
    tmp_path, old, new, extra, expected_ids, expected_extra
):
    template, source = synthetic_files(tmp_path)
    template.write_bytes(f'"VendorID" "{old[0]}"\n"DeviceID" "{old[1]}"\n{extra}\n'.encode())
    source.write_bytes(f'"VendorID" "{new[0]}"\n"DeviceID" "{new[1]}"\n'.encode())
    before, source_before = template.read_bytes(), source.read_bytes()
    proposal = video.preview_video_template(template, source, "legacy")
    expected = f'"VendorID" "{expected_ids[0]}"\n"DeviceID" "{expected_ids[1]}"\n{expected_extra}\n'
    assert proposal.changed
    assert (proposal.source_vendor_id, proposal.source_device_id) == new
    assert (proposal.after_vendor_id, proposal.after_device_id) == expected_ids
    assert proposal.rendered_content == expected
    applied = video.apply_video_template(proposal)
    assert template.read_bytes() == expected.encode()
    assert applied.backup_path.read_bytes() == before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("which", ["template", "source"])
@pytest.mark.parametrize("mutation", ["mtime_only", "same_size_and_mtime", "same_bytes_new_inode"])
def test_snapshot_rejects_metadata_or_hidden_content_changes(tmp_path, which, mutation):
    template, source = synthetic_files(tmp_path)
    proposal = video.preview_video_template(template, source)
    target = template if which == "template" else source
    snapshot = target.stat()
    if mutation == "mtime_only":
        os.utime(target, ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns + 5_000_000_000))
        assert target.stat().st_mtime_ns != snapshot.st_mtime_ns
    elif mutation == "same_size_and_mtime":
        target.write_bytes(target.read_bytes().replace(b"0", b"9"))
        os.utime(target, ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
        assert target.stat().st_size == snapshot.st_size
        assert target.stat().st_mtime_ns == snapshot.st_mtime_ns
    else:
        replacement = tmp_path / "replacement.txt"
        replacement.write_bytes(target.read_bytes())
        os.utime(replacement, ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
        os.replace(replacement, target)
        assert target.stat().st_ino != snapshot.st_ino
    expected = bytes_in(tmp_path)
    with pytest.raises(video.VideoStalePreviewError):
        video.apply_video_template(proposal)
    assert bytes_in(tmp_path) == expected


def test_noop_preview_rejects_source_comment_change_without_ids_changing(tmp_path):
    template, source = synthetic_files(tmp_path, equal=True)
    proposal = video.preview_video_template(template, source)
    assert not proposal.changed
    source.write_bytes(source.read_bytes().replace(b"keep", b"edit"))
    expected = bytes_in(tmp_path)
    with pytest.raises(video.VideoStalePreviewError):
        video.apply_video_template(proposal)
    assert bytes_in(tmp_path) == expected


def test_new_original_distribution_marker_invalidates_existing_proposal(tmp_path):
    template, source = synthetic_files(tmp_path)
    proposal = video.preview_video_template(template, source)
    (tmp_path / "Panel.exe").write_bytes(b"inert marker; never executed")
    expected = bytes_in(tmp_path)
    with pytest.raises(video.VideoStalePreviewError):
        video.apply_video_template(proposal)
    assert bytes_in(tmp_path) == expected


def test_template_becoming_readonly_is_stale_without_permission_repair(tmp_path):
    template, source = synthetic_files(tmp_path)
    proposal = video.preview_video_template(template, source)
    template.chmod(stat.S_IREAD)
    expected = bytes_in(tmp_path)
    try:
        with pytest.raises(video.VideoStalePreviewError):
            video.apply_video_template(proposal)
        assert not template.stat().st_mode & stat.S_IWUSR
        assert bytes_in(tmp_path) == expected
    finally:
        template.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_readonly_hardlinked_source_is_preserved_while_template_changes(tmp_path):
    template, source = synthetic_files(tmp_path)
    source_alias = tmp_path / "source alias.txt"
    os.link(source, source_alias)
    source.chmod(stat.S_IREAD)
    source_before, snapshot = source.read_bytes(), source.stat()
    try:
        applied = video.apply_video_template(video.preview_video_template(template, source))
        assert applied.changed
        assert source.read_bytes() == source_alias.read_bytes() == source_before
        assert source.stat().st_ino == snapshot.st_ino
        assert source.stat().st_mtime_ns == snapshot.st_mtime_ns
        assert source.stat().st_mode == snapshot.st_mode
    finally:
        source.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_two_threads_cannot_both_apply_one_reviewed_snapshot(tmp_path):
    template, source = synthetic_files(tmp_path)
    before, source_before = template.read_bytes(), source.read_bytes()
    proposal = video.preview_video_template(template, source)
    start = threading.Barrier(2)

    def attempt():
        start.wait(timeout=5)
        try:
            return video.apply_video_template(proposal)
        except video.VideoStalePreviewError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=5) for future in attempts]
    applied = [result for result in results if isinstance(result, video.VideoApplyResult)]
    stale = [result for result in results if isinstance(result, video.VideoStalePreviewError)]
    assert len(applied) == len(stale) == 1
    assert applied[0].changed
    assert applied[0].backup_path.read_bytes() == before
    assert len(list(tmp_path.glob("*.bak"))) == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("failure", ["before_replace", "after_replace", "rollback"])
def test_cli_never_reports_success_after_incomplete_apply(tmp_path, monkeypatch, capsys, failure):
    template, source = synthetic_files(tmp_path)
    before, source_before = template.read_bytes(), source.read_bytes()
    real_replace = os.replace
    calls = 0

    def fail_replace(staged, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure != "before_replace":
                real_replace(staged, destination)
            raise OSError("synthetic private diagnostic that must not be printed")
        if failure == "rollback":
            raise OSError("synthetic private rollback diagnostic")
        return real_replace(staged, destination)

    monkeypatch.setattr(video.os, "replace", fail_replace)
    assert maintenance.main(cli_arguments(template, source) + ["--apply"]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["error"] == (
        "video_rollback_failed" if failure == "rollback" else "video_apply_failed"
    )
    assert result["rollback_succeeded"] is (failure != "rollback")
    assert Path(result["backup_path"]).read_bytes() == before
    assert "operation" not in result and "changed" not in result
    assert "synthetic private" not in captured.out + captured.err
    assert not list(tmp_path.glob("*.tmp"))
    assert source.read_bytes() == source_before
    if failure != "rollback":
        assert template.read_bytes() == before


def test_cleanup_error_does_not_hide_retained_backup_from_cli(tmp_path, monkeypatch, capsys):
    template, source = synthetic_files(tmp_path)
    before, source_before = template.read_bytes(), source.read_bytes()
    real_unlink = Path.unlink

    def fail_replace(*args, **kwargs):
        raise PermissionError("synthetic replace denial")

    def fail_temporary_cleanup(path, *args, **kwargs):
        if path.parent == tmp_path and path.suffix == ".tmp" and path.exists():
            raise PermissionError("synthetic staging cleanup denial")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(video.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    assert maintenance.main(cli_arguments(template, source) + ["--apply"]) == 2
    result = json.loads(capsys.readouterr().out)
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert template.read_bytes() == before and source.read_bytes() == source_before
    assert result["error"] == "video_apply_failed"
    assert result["rollback_succeeded"] is True
    assert Path(result["backup_path"]) == backups[0]


def test_cli_source_hash_rejects_changed_source_even_when_new_ids_match(tmp_path, capsys):
    template, source = synthetic_files(tmp_path, equal=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_bytes(source.read_bytes() + b"// new comment\n")
    expected = bytes_in(tmp_path)
    assert (
        maintenance.main(
            cli_arguments(template, source) + ["--apply", "--expect-source-sha256", source_hash]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {"error": "video_preview_stale"}
    assert bytes_in(tmp_path) == expected


def test_cli_module_entrypoint_returns_json_failure_and_nonzero_exit(tmp_path):
    template, source = synthetic_files(tmp_path)
    source.unlink()
    before = template.read_bytes()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsm_panel.maintenance",
            *cli_arguments(template, source),
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {"error": "video_file_read_failed"}
    assert not completed.stderr
    assert bytes_in(tmp_path) == {template.name: before}


@pytest.fixture
def independent_panel(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    owner = SimpleNamespace(bridge=AsyncBridge(), _closing=False)
    widget = VideoPanel(owner)
    qtbot.addWidget(widget)
    widget.show()
    template, source = synthetic_files(tmp_path)
    widget.template_path.setText(str(template))
    widget.steam_path.setText(str(source))
    yield widget, template, source
    owner._closing = True
    owner.bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=5000)


def wait_for_video(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=5000)


def test_file_dialog_targets_correct_field_and_cancel_preserves_preview(
    qtbot, independent_panel, monkeypatch
):
    widget, template, source = independent_panel
    widget.preview_button.click()
    wait_for_video(qtbot, widget)
    reviewed = widget.preview
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    widget.browse_buttons[0].click()
    assert widget.preview is reviewed and widget.apply_button.isEnabled()
    replacement = source.with_name("second synthetic source.txt")
    replacement.write_bytes(source.read_bytes())
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(replacement), ""))
    widget.browse_buttons[1].click()
    assert widget.template_path.text() == str(template)
    assert widget.steam_path.text() == str(replacement)
    assert widget.preview is None and not widget.apply_button.isEnabled()


def test_changed_selection_discards_late_preview(qtbot, independent_panel, monkeypatch):
    widget, template, source = independent_panel
    original = video_ui.preview_video_template
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("synthetic timeout")
        return result

    monkeypatch.setattr(video_ui, "preview_video_template", delayed)
    widget.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        assert not widget.template_path.isEnabled()
        assert not any(button.isEnabled() for button in widget.browse_buttons)
        widget.steam_path.setText(str(source.with_name("different.txt")))
    finally:
        release.set()
    wait_for_video(qtbot, widget)
    assert widget.preview is None and not widget.apply_button.isEnabled()
    assert "Выбор файлов изменился" in widget.status.text()
    assert not widget.result.toPlainText()
    assert template.read_bytes().startswith(b'"VendorID" "10"')


def test_ui_reports_failed_rollback_and_backup_without_success(
    qtbot, independent_panel, monkeypatch
):
    widget, template, source = independent_panel
    before, source_before = template.read_bytes(), source.read_bytes()
    widget.preview_button.click()
    wait_for_video(qtbot, widget)
    real_replace = video.os.replace
    calls = 0

    def fail_replace(staged, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_replace(staged, destination)
        raise OSError("synthetic sensitive diagnostic")

    monkeypatch.setattr(video.os, "replace", fail_replace)
    widget.apply_button.click()
    wait_for_video(qtbot, widget)
    backup = next(template.parent.glob("*.bak"))
    assert "video_rollback_failed" in widget.status.text()
    assert str(backup) in widget.status.text()
    assert "Шаблон сохранён" not in widget.status.text()
    assert "synthetic sensitive" not in widget.status.text()
    assert backup.read_bytes() == before and source.read_bytes() == source_before
    assert widget.preview is None and not widget.apply_button.isEnabled()
    assert widget.preview_button.isEnabled() and widget.template_path.isEnabled()


def test_main_window_close_waits_for_real_template_apply_and_keeps_qt_responsive(
    qtbot, qapp, tmp_path, monkeypatch
):
    qapp.setQuitOnLastWindowClosed(False)
    template, source = synthetic_files(tmp_path)
    before, source_before = template.read_bytes(), source.read_bytes()
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    real_replace = video.os.replace
    worker_threads = []

    def delayed_replace(staged, destination):
        if Path(destination) == template:
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic timeout")
        result = real_replace(staged, destination)
        if Path(destination) == template:
            committed.set()
        return result

    with AccountRepository(tmp_path / "synthetic.sqlite") as repository:
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        try:
            window.show()
            qtbot.waitUntil(lambda: window.ready)
            widget = window.video
            widget.template_path.setText(str(template))
            widget.steam_path.setText(str(source))
            widget.preview_button.click()
            wait_for_video(qtbot, widget)
            monkeypatch.setattr(video.os, "replace", delayed_replace)
            widget.apply_button.click()
            qtbot.waitUntil(entered.is_set)
            window.close()
            tick = []
            QTimer.singleShot(0, lambda: tick.append(threading.get_ident()))
            qtbot.waitUntil(lambda: bool(tick))
            assert tick == [threading.get_ident()]
            assert worker_threads and worker_threads[0] != threading.get_ident()
            assert window._closing and not window._closed
            assert not window.tabs.isEnabled()
            widget._apply()
            assert len(worker_threads) == 1
            release.set()
            qtbot.waitUntil(lambda: window._closed, timeout=5000)
            assert committed.is_set()
            assert not window.bridge._thread.is_alive()
            assert window.bridge.shutdown_error is None
            assert template.read_bytes() == (
                b'"VendorID" "30"\r\n"DeviceID" "40"\r\n// keep 10 and 20\r\n'
            )
            assert next(tmp_path.glob("*.bak")).read_bytes() == before
            assert source.read_bytes() == source_before
        finally:
            release.set()
            if not window._closed:
                window.close()
                qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()
