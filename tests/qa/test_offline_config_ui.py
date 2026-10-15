"""Independent desktop integration checks with synthetic, isolated config trees."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from fsm_panel.adapters import account_config as adapter
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui import config_files as config_ui
from fsm_panel.ui.window import MainWindow

PRIVATE_SETTING = "QA_SETTINGS_CONTENT_SENTINEL"
PRIVATE_FILE = "QA_CONFIG_CONTENT_SENTINEL"
PRIVATE_ERROR = "QA_EXTERNAL_EXCEPTION_SENTINEL"


def tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def ready(qtbot, panel):
    qtbot.waitUntil(lambda: panel._future is None, timeout=10000)


def diagnostics(window):
    panel = window.config_files
    return "\n".join(
        (
            window.statusBar().currentMessage(),
            window.log.toPlainText(),
            panel.workspace_status.text(),
            panel.settings_status.text(),
            panel.account.status.text(),
            panel.autoexec.status.text(),
            panel.account.summary.toPlainText(),
            panel.autoexec.summary.toPlainText(),
        )
    )


@pytest.fixture
def window(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    repository = AccountRepository(tmp_path / "synthetic.sqlite")
    coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
    widget = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
    widget.show()
    qtbot.waitUntil(lambda: widget.ready)
    try:
        yield widget
    finally:
        widget.close()
        qtbot.waitUntil(lambda: widget._closed, timeout=10000)
        assert not widget.bridge._thread.is_alive()
        widget.deleteLater()
        repository.close()


@pytest.fixture
def files(qtbot, window, tmp_path):
    panel = window.config_files
    window.tabs.setCurrentWidget(panel)
    root = tmp_path / "isolated"
    panel.workspace_path.setText(str(root))
    panel.create_workspace_button.click()
    ready(qtbot, panel)
    assert panel.workspace is not None
    settings = tmp_path / "explicit-settings.json"
    settings.write_text(
        json.dumps(
            {
                "STEAM_PATH": r"Z:\synthetic-only\steam.exe",
                "DISABLE_STEAM_OVERLAY": True,
                "NO_AVAST_MODE": False,
                "TG_BOT_API_KEY": PRIVATE_SETTING,
            }
        ),
        encoding="utf-8",
    )
    video, machine, localcopy, autoexec = (
        tmp_path / name for name in ("video", "machine", "local-copy", "autoexec-source")
    )
    video.write_bytes(b"synthetic video\n")
    machine.write_bytes(b"synthetic machine\n")
    localcopy.write_bytes((PRIVATE_FILE + "\r\n").encode())
    autoexec.write_bytes(b"synthetic autoexec\n")
    panel.settings_path.setText(str(settings))
    panel.import_settings_button.click()
    ready(qtbot, panel)
    assert panel.imported_settings is not None
    panel.steam_id.setText("76561197960265770")
    panel.login.setText("synthetic_ui_account")
    panel.video_source.setText(str(video))
    panel.machine_source.setText(str(machine))
    panel.localconfig_copy.setText(str(localcopy))
    panel.timestamp.setText("1700000000.5")
    panel.autoexec_source.setText(str(autoexec))
    panel.config_file_name.setText("legacy.cfg")
    target = root / "steam/userdata/42"
    target.mkdir()
    (target / "unchanged").write_bytes(b"original unrelated account data")
    game = root / "game/game/csgo/cfg"
    game.mkdir(parents=True)
    (game / "autoexec.cfg").write_bytes(b"old autoexec\r\n")
    (game / "legacy.cfg").write_bytes(b"original legacy file\r\n")
    return SimpleNamespace(
        window=window,
        panel=panel,
        root=root,
        target=target,
        game=game,
        settings=settings,
        video=video,
        machine=machine,
        localcopy=localcopy,
        autoexec=autoexec,
    )


def preview(qtbot, panel, kind="account"):
    controls = getattr(panel, kind)
    controls.preview_button.click()
    ready(qtbot, panel)
    assert getattr(panel, kind + "_review") is not None
    assert controls.apply_button.isEnabled()


def test_main_window_settings_never_supply_or_replace_explicit_copy_settings(qtbot, files):
    window, panel = files.window, files.panel
    assert panel.bridge is window.bridge
    assert not panel._owns_bridge
    assert window.tabs.tabText(window.tabs.indexOf(panel)) == "Конфиги игры"
    window.settings.set("NO_AVAST_MODE", True)
    window.settings.set("DISABLE_STEAM_OVERLAY", False)
    before_settings = window.settings.to_dict(include_secrets=True)
    panel.settings_path.setText("")
    assert panel.imported_settings is None
    assert not panel.account.preview_button.isEnabled()
    assert not panel.account.apply_button.isEnabled()
    panel.settings_path.setText(str(files.settings))
    panel.import_settings_button.click()
    ready(qtbot, panel)
    preview(qtbot, panel)
    before_sources = {path: path.read_bytes() for path in (files.settings, files.localcopy)}
    panel.account.apply_button.click()
    ready(qtbot, panel)
    assert (files.target / "config/localconfig.vdf").read_bytes() == files.localcopy.read_bytes()
    assert window.settings.to_dict(include_secrets=True) == before_settings
    assert {path: path.read_bytes() for path in before_sources} == before_sources
    assert PRIVATE_SETTING not in diagnostics(window)
    assert PRIVATE_FILE not in diagnostics(window)


@pytest.mark.parametrize("field", ["login", "config_file_name", "settings_path", "workspace_path"])
def test_edit_after_both_previews_revokes_both_apply_capabilities(qtbot, files, field):
    panel = files.panel
    preview(qtbot, panel)
    preview(qtbot, panel, "autoexec")
    before = tree(files.root)
    control = getattr(panel, field)
    control.setText(control.text() + "changed")
    assert panel.account_review is None and panel.autoexec_review is None
    assert not panel.account.apply_button.isEnabled()
    assert not panel.autoexec.apply_button.isEnabled()
    panel.account.apply_button.click()
    panel.autoexec.apply_button.click()
    assert panel._future is None
    assert tree(files.root) == before
    assert panel.account.summary.toPlainText() == ""
    assert panel.autoexec.summary.toPlainText() == ""


def test_changed_selection_during_import_cannot_install_the_old_settings_snapshot(
    qtbot, files, tmp_path, monkeypatch
):
    panel = files.panel
    second = tmp_path / "second-settings.json"
    replacement = json.loads(files.settings.read_text())
    replacement["NO_AVAST_MODE"] = True
    second.write_text(json.dumps(replacement))
    entered, release = threading.Event(), threading.Event()
    native_load = config_ui._load_settings

    def delayed(*args):
        result = native_load(*args)
        if Path(args[0]) == files.settings:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(config_ui, "_load_settings", delayed)
    panel.import_settings_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        assert not panel.settings_path.isEnabled()
        panel.settings_path.setText(str(second))
    finally:
        release.set()
    ready(qtbot, panel)
    assert panel.imported_settings is None
    assert not panel.account.preview_button.isEnabled()
    panel.import_settings_button.click()
    ready(qtbot, panel)
    existing = files.target / "config/localconfig.vdf"
    existing.parent.mkdir()
    existing.write_bytes(b"preserve existing localconfig\r\n")
    preview(qtbot, panel)
    panel.account.apply_button.click()
    ready(qtbot, panel)
    assert existing.read_bytes() == b"preserve existing localconfig\r\n"
    assert PRIVATE_SETTING not in diagnostics(files.window)


def test_other_panel_failure_cannot_release_the_config_lock_or_leak_exception_text(
    qtbot, files, monkeypatch
):
    panel = files.panel
    entered, release = threading.Event(), threading.Event()
    native_preview = config_ui._account_preview

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return native_preview(*args)

    async def unrelated_failure():
        raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(config_ui, "_account_preview", delayed)
    panel.account.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    pending = panel._future
    try:
        other = files.window.bridge.submit(unrelated_failure())
        qtbot.waitUntil(other.done)
        qtbot.waitUntil(lambda: "RuntimeError" in files.window.log.toPlainText())
        assert panel._future is pending and not pending.done()
        assert not panel.account.preview_button.isEnabled()
        assert not panel.autoexec.preview_button.isEnabled()
        assert PRIVATE_ERROR not in diagnostics(files.window)
    finally:
        release.set()
    ready(qtbot, panel)
    assert panel.account_review is not None
    assert panel.account.apply_button.isEnabled()


@pytest.mark.parametrize("name", ["settings", "localcopy", "video", "machine"])
def test_unchanged_bytes_with_new_identity_cannot_apply_an_old_ui_snapshot(qtbot, files, name):
    preview(qtbot, files.panel)
    selected = getattr(files, name)
    original = selected.stat()
    replacement = selected.with_suffix(".replacement")
    replacement.write_bytes(selected.read_bytes())
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, selected)
    assert selected.stat().st_ino != original.st_ino
    before = tree(files.root)
    files.panel.account.apply_button.click()
    ready(qtbot, files.panel)
    assert "config_preview_stale" in files.panel.account.status.text()
    assert tree(files.root) == before
    assert not files.panel.account.apply_button.isEnabled()
    assert PRIVATE_FILE not in diagnostics(files.window)


def test_destination_edit_during_staging_survives_rejected_ui_apply(qtbot, files, monkeypatch):
    preview(qtbot, files.panel)
    native_copy = adapter.shutil.copytree
    edited = []

    def copy_then_edit(source, destination, *args, **kwargs):
        result = native_copy(source, destination, *args, **kwargs)
        if Path(destination).parent == files.root:
            (files.target / "concurrent-new").write_bytes(b"preserve concurrent data")
            edited.append(True)
        return result

    before = tree(files.target)
    monkeypatch.setattr(adapter.shutil, "copytree", copy_then_edit)
    files.panel.account.apply_button.click()
    ready(qtbot, files.panel)
    assert edited == [True]
    assert "config_preview_stale" in files.panel.account.status.text()
    assert tree(files.target) == {**before, "concurrent-new": b"preserve concurrent data"}
    assert not list(files.root.glob(".config-stage-*"))
    assert not list(files.root.glob(".config-backup-*"))


@pytest.mark.parametrize("rollback_denied", [False, True])
def test_real_failed_directory_install_reports_the_remaining_recovery_tree(
    qtbot, files, monkeypatch, rollback_denied
):
    preview(qtbot, files.panel)
    before = tree(files.target)
    native_replace = adapter._replace_bounded

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise PermissionError(PRIVATE_ERROR)
        if rollback_denied and source.name.startswith(".config-backup-"):
            raise PermissionError(PRIVATE_SETTING)
        return native_replace(source, destination, workspace)

    monkeypatch.setattr(adapter, "_replace_bounded", fail_install)
    files.panel.account.apply_button.click()
    ready(qtbot, files.panel)
    status = files.panel.account.status.text()
    backups = list(files.root.glob(".config-backup-*"))
    if rollback_denied:
        assert "config_rollback_failed" in status
        assert len(backups) == 1 and str(backups[0]) in status
        assert tree(backups[0]) == before
        assert not files.target.exists()
    else:
        assert "config_apply_failed" in status
        assert "Исходная копия восстановлена" in status
        assert tree(files.target) == before and backups == []
    assert not list(files.root.glob(".config-stage-*"))
    assert "Копия сохранена" not in status
    assert not files.panel.account.apply_button.isEnabled()
    assert PRIVATE_ERROR not in diagnostics(files.window)
    assert PRIVATE_SETTING not in diagnostics(files.window)


@pytest.mark.parametrize("license_cleanup_fails", [False, True])
@pytest.mark.parametrize("source_changes", [False, True])
def test_main_window_close_waits_for_actual_tree_staging_even_after_cleanup_error(
    qtbot, files, monkeypatch, license_cleanup_fails, source_changes
):
    window, panel = files.window, files.panel
    preview(qtbot, panel)
    before = tree(files.target)
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    native_copy = adapter.shutil.copytree

    def delayed_copy(source, destination, *args, **kwargs):
        result = native_copy(source, destination, *args, **kwargs)
        if Path(destination).parent == files.root:
            worker_threads.append(threading.current_thread())
            entered.set()
            assert release.wait(5)
        return result

    class FailingLicense:
        async def close(self):
            raise RuntimeError(PRIVATE_ERROR)

    if license_cleanup_fails:
        window.license_runtime = FailingLicense()
    monkeypatch.setattr(adapter.shutil, "copytree", delayed_copy)
    panel.account.apply_button.click()
    qtbot.waitUntil(entered.is_set)
    pending = panel._future
    try:
        window.close()
        window.close()
        qtbot.waitUntil(lambda: panel._closing)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append("responsive"))
        qtbot.waitUntil(lambda: bool(ticks))
        assert window._closing and not window._closed
        assert not pending.done()
        assert window.bridge._thread.is_alive()
        assert worker_threads[0].ident != threading.get_ident()
        assert tree(files.target) == before
        request = panel._request
        panel._preview("account")
        panel._apply("account")
        assert panel._request == request
        if source_changes:
            files.localcopy.write_bytes(b"changed while closing\n")
    finally:
        release.set()
    qtbot.waitUntil(lambda: window._closed, timeout=10000)
    qtbot.waitUntil(lambda: not worker_threads[0].is_alive(), timeout=10000)
    assert pending.done() and not pending.cancelled() and pending.exception() is None
    assert not window.bridge._thread.is_alive()
    assert window.bridge.shutdown_error == ("RuntimeError" if license_cleanup_fails else None)
    backups = list(files.root.glob(".config-backup-*"))
    if source_changes:
        assert tree(files.target) == before and backups == []
        assert "config_preview_stale" in panel.account.status.text()
    else:
        assert len(backups) == 1 and tree(backups[0]) == before
        assert (
            files.target / "config/localconfig.vdf"
        ).read_bytes() == files.localcopy.read_bytes()
    assert not list(files.root.glob(".config-stage-*"))
    assert PRIVATE_ERROR not in diagnostics(window)


def test_closing_config_page_preserves_shared_bridge_until_main_window_closes(qtbot, files):
    panel, window = files.panel, files.window
    panel.close()
    qtbot.waitUntil(lambda: panel._closed)
    assert window.bridge._thread.is_alive()
    window.add_sample()
    window.table.selectRow(0)
    window.start_selected()
    qtbot.waitUntil(lambda: window.table.item(0, 2).text() == "running", timeout=5000)
    window.close()
    qtbot.waitUntil(lambda: window._closed, timeout=5000)
    assert window.repository.list_accounts()[0].state.value == "stopped"
    assert window.bridge.shutdown_error is None
