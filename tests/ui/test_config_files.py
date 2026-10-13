import json
import threading
import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QTimer

from fsm_panel.adapters import account_config as adapter
from fsm_panel.adapters.account_config import ConfigApplyError
from fsm_panel.ui import config_files as module
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.config_files import ConfigFilesWidget


def wait_ready(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=10000)


def visible_output(widget):
    return "\n".join(
        (
            widget.workspace_status.text(),
            widget.settings_status.text(),
            widget.account.status.text(),
            widget.autoexec.status.text(),
            widget.account.summary.toPlainText(),
            widget.autoexec.summary.toPlainText(),
        )
    )


@pytest.fixture
def panel(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    bridge = AsyncBridge()
    widget = ConfigFilesWidget(bridge)
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not bridge._thread.is_alive(), timeout=10000)
    widget._closed = True


@pytest.fixture
def account(qtbot, panel, tmp_path):
    widget = panel
    root = tmp_path / "copy"
    widget.workspace_path.setText(str(root))
    widget.create_workspace_button.click()
    wait_ready(qtbot, widget)
    assert widget.workspace is not None
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "STEAM_PATH": r"C:\logical-only\steam.exe",
                "DISABLE_STEAM_OVERLAY": True,
                "NO_AVAST_MODE": False,
                "unknown_private_key": "private-setting-value",
            }
        )
    )
    video = tmp_path / "video.txt"
    machine = tmp_path / "machine.vcfg"
    localconfig = tmp_path / "localconfig.vdf"
    video.write_bytes(b"private-video\r\n")
    machine.write_bytes(b"private-machine\n")
    localconfig.write_bytes(b"private-localconfig\r\n")
    widget.settings_path.setText(str(settings))
    widget.import_settings_button.click()
    wait_ready(qtbot, widget)
    assert widget.imported_settings is not None
    widget.steam_id.setText("76561197960265770")
    widget.login.setText("private-login")
    widget.video_source.setText(str(video))
    widget.machine_source.setText(str(machine))
    widget.localconfig_copy.setText(str(localconfig))
    widget.timestamp.setText("1234.5")
    return widget, root, settings, video, machine, localconfig


def account_preview(qtbot, widget):
    widget.account.preview_button.click()
    wait_ready(qtbot, widget)
    assert widget.account_review is not None


def prepare_autoexec(account):
    widget, root, _, video, _, _ = account
    cfg = root / "game/game/csgo/cfg"
    cfg.mkdir(parents=True)
    (cfg / "autoexec.cfg").write_bytes(b"old-autoexec\n")
    (cfg / "legacy.cfg").write_bytes(b"old-legacy\n")
    (cfg / "keep.cfg").write_bytes(b"untouched\r\n")
    widget.tabs.setCurrentIndex(1)
    widget.autoexec_source.setText(str(video))
    widget.config_file_name.setText("legacy.cfg")
    return cfg


def test_initial_controls_require_workspace_and_explicit_settings(panel):
    assert panel.workspace is None
    assert panel.imported_settings is None
    assert panel.settings_path.text() == ""
    assert panel.timestamp.text() == ""
    assert panel.config_file_name.text() == ""
    assert not panel.import_settings_button.isEnabled()
    assert not panel.account.preview_button.isEnabled()
    assert not panel.account.apply_button.isEnabled()
    assert not panel.autoexec.preview_button.isEnabled()
    assert not panel.autoexec.apply_button.isEnabled()


def test_workspace_create_open_and_refuse_unmarked_existing_root(qtbot, panel, tmp_path):
    root = tmp_path / "new-copy"
    panel.workspace_path.setText(str(root))
    panel.create_workspace_button.click()
    wait_ready(qtbot, panel)
    workspace = panel.workspace
    assert workspace is not None
    assert (root / ".fsm-config-workspace.json").exists()
    panel.open_workspace_button.click()
    wait_ready(qtbot, panel)
    assert panel.workspace == workspace
    plain = tmp_path / "unmarked"
    plain.mkdir()
    panel.workspace_path.setText(str(plain))
    assert panel.workspace is None
    panel.open_workspace_button.click()
    wait_ready(qtbot, panel)
    assert panel.workspace is None
    assert "config_workspace_marker_required" in panel.workspace_status.text()
    panel.create_workspace_button.click()
    wait_ready(qtbot, panel)
    assert "config_new_workspace_required" in panel.workspace_status.text()
    assert list(plain.iterdir()) == []


def test_account_preview_apply_backup_and_no_raw_content(qtbot, account):
    widget, root, settings, video, machine, localconfig = account
    target = root / "steam/userdata/42"
    old = target / "other/keep"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"keep-exact")
    source_bytes = [path.read_bytes() for path in (settings, video, machine, localconfig)]
    account_preview(qtbot, widget)
    reviewed = widget.account_review
    assert reviewed is not None
    assert reviewed.fingerprint in widget.account.summary.toPlainText()
    assert "Записей: 7; удалений: 3" in widget.account.summary.toPlainText()
    assert widget.account.apply_button.isEnabled()
    assert not (target / "730").exists()
    for secret in (
        "private-login",
        "private-video",
        "private-machine",
        "private-localconfig",
        "private-setting-value",
    ):
        assert secret not in visible_output(widget)
    widget.account.apply_button.click()
    wait_ready(qtbot, widget)
    assert (target / "730/local/cfg/cs2_video.txt").read_bytes() == b"private-video\r\n"
    assert (target / "config/localconfig.vdf").read_bytes() == b"private-localconfig\r\n"
    assert old.read_bytes() == b"keep-exact"
    backups = list(root.glob(".config-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "other/keep").read_bytes() == b"keep-exact"
    assert str(backups[0]) in widget.account.status.text()
    assert source_bytes == [path.read_bytes() for path in (settings, video, machine, localconfig)]
    assert widget.account_review is None
    assert not widget.account.apply_button.isEnabled()
    assert widget.account.summary.toPlainText() == ""


@pytest.mark.parametrize(
    "field",
    [
        "steam_id",
        "login",
        "video_source",
        "machine_source",
        "timestamp",
        "localconfig_copy",
        "force",
        "account_encoding",
        "settings_path",
        "workspace_path",
    ],
)
def test_editing_any_account_input_invalidates_fingerprint(qtbot, account, field):
    widget = account[0]
    account_preview(qtbot, widget)
    control = getattr(widget, field)
    if field == "force":
        control.setChecked(True)
    elif field == "account_encoding":
        control.setCurrentText("cp1251")
    else:
        control.setText(control.text() + "changed")
    assert widget.account_review is None
    assert not widget.account.apply_button.isEnabled()
    assert widget.account.summary.toPlainText() == ""


@pytest.mark.parametrize("which", ["settings", "video", "machine", "localconfig", "target"])
def test_external_change_after_preview_rejects_apply_and_clears_fingerprint(qtbot, account, which):
    widget, root, settings, video, machine, localconfig = account
    account_preview(qtbot, widget)
    if which == "target":
        target = root / "steam/userdata/42"
        target.mkdir()
        (target / "external").write_bytes(b"external-value")
    else:
        selected = {
            "settings": settings,
            "video": video,
            "machine": machine,
            "localconfig": localconfig,
        }[which]
        selected.write_bytes(selected.read_bytes() + b" ")
    widget.account.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_preview_stale" in widget.account.status.text()
    assert not widget.account.apply_button.isEnabled()
    assert widget.account.summary.toPlainText() == ""
    assert not (root / "steam/userdata/42/730").exists()
    assert not list(root.glob(".config-backup-*"))


def test_settings_changed_after_import_requires_reimport(qtbot, account):
    widget, _, settings, *_ = account
    settings.write_bytes(settings.read_bytes() + b" ")
    widget.account.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_preview_stale" in widget.account.status.text()
    assert widget.account_review is None
    widget.import_settings_button.click()
    wait_ready(qtbot, widget)
    account_preview(qtbot, widget)


@pytest.mark.parametrize("source_index", [2, 5])
def test_auxiliary_change_during_staging_is_rejected_before_commit(
    qtbot, account, monkeypatch, source_index
):
    widget, root, *_ = account
    target = root / "steam/userdata/42"
    target.mkdir()
    (target / "keep").write_bytes(b"existing")
    account_preview(qtbot, widget)
    original_copy = adapter.shutil.copytree
    changed = False

    def copying(*args, **kwargs):
        nonlocal changed
        result = original_copy(*args, **kwargs)
        if not changed:
            changed = True
            source = account[source_index]
            source.write_bytes(source.read_bytes() + b" ")
        return result

    monkeypatch.setattr(adapter.shutil, "copytree", copying)
    widget.account.apply_button.click()
    wait_ready(qtbot, widget)
    assert changed
    assert "config_preview_stale" in widget.account.status.text()
    assert (target / "keep").read_bytes() == b"existing"
    assert not (target / "730").exists()
    assert not list(root.glob(".config-backup-*"))
    assert not widget.account.apply_button.isEnabled()


@pytest.mark.parametrize(
    "content,code",
    [
        ("{}", "config_required_settings_missing"),
        ('{"NO_AVAST_MODE":"False"}', "config_invalid_settings"),
        ("not-json-private-error", "config_invalid_settings"),
    ],
)
def test_explicit_settings_import_has_no_legacy_defaults_or_raw_errors(
    qtbot, account, content, code
):
    widget, _, settings, *_ = account
    settings.write_text(content)
    widget.import_settings_button.click()
    wait_ready(qtbot, widget)
    assert widget.imported_settings is None
    assert code in widget.settings_status.text()
    assert "not-json-private-error" not in visible_output(widget)
    assert not widget.account.preview_button.isEnabled()


def test_auxiliary_inputs_inside_workspace_are_rejected(qtbot, account):
    widget, root, settings, *_ = account
    copied_settings = root / "settings.json"
    copied_settings.write_bytes(settings.read_bytes())
    widget.settings_path.setText(str(copied_settings))
    widget.import_settings_button.click()
    wait_ready(qtbot, widget)
    assert "config_source_destination_overlap" in widget.settings_status.text()
    widget.settings_path.setText(str(settings))
    widget.import_settings_button.click()
    wait_ready(qtbot, widget)
    inside = root / "localconfig-copy"
    inside.write_bytes(b"private-local")
    widget.localconfig_copy.setText(str(inside))
    widget.account.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_source_destination_overlap" in widget.account.status.text()
    assert inside.read_bytes() == b"private-local"


@pytest.mark.parametrize(
    "field,value",
    [
        ("steam_id", "private-invalid-number"),
        ("timestamp", "nan"),
        ("video_source", ""),
        ("login", ""),
    ],
)
def test_invalid_inputs_surface_only_safe_codes(qtbot, account, field, value):
    widget = account[0]
    getattr(widget, field).setText(value)
    widget.account.preview_button.click()
    wait_ready(qtbot, widget)
    assert widget.account_review is None
    assert "Операция не выполнена: config_" in widget.account.status.text()
    assert "private-invalid-number" not in visible_output(widget)


def test_autoexec_preview_apply_preserves_other_files_and_reports_backup(qtbot, account):
    widget = account[0]
    cfg = prepare_autoexec(account)
    widget.autoexec.preview_button.click()
    wait_ready(qtbot, widget)
    assert widget.autoexec_review is not None
    assert widget.autoexec.apply_button.isEnabled()
    assert "Записей: 1; удалений: 2" in widget.autoexec.summary.toPlainText()
    assert (cfg / "legacy.cfg").exists()
    widget.autoexec.apply_button.click()
    wait_ready(qtbot, widget)
    assert (cfg / "autoexec.cfg").read_bytes() == b"private-video\r\n"
    assert not (cfg / "legacy.cfg").exists()
    assert (cfg / "keep.cfg").read_bytes() == b"untouched\r\n"
    assert "Резервная копия:" in widget.autoexec.status.text()
    assert not widget.autoexec.apply_button.isEnabled()


def test_autoexec_equal_preview_preserves_timestamps_and_legacy_file(qtbot, account):
    widget = account[0]
    cfg = prepare_autoexec(account)
    (cfg / "autoexec.cfg").write_bytes(b"private-video\n")
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in cfg.iterdir()
    }
    widget.autoexec.preview_button.click()
    wait_ready(qtbot, widget)
    assert widget.autoexec_review is not None
    assert not widget.autoexec.apply_button.isEnabled()
    assert "Текст совпадает" in widget.autoexec.status.text()
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in cfg.iterdir()
    }


@pytest.mark.parametrize("field", ["autoexec_source", "config_file_name", "autoexec_encoding"])
def test_autoexec_input_edit_invalidates_preview(qtbot, account, field):
    widget = account[0]
    prepare_autoexec(account)
    widget.autoexec.preview_button.click()
    wait_ready(qtbot, widget)
    control = getattr(widget, field)
    if field.endswith("encoding"):
        control.setCurrentText("cp1251")
    else:
        control.setText(control.text() + "x")
    assert widget.autoexec_review is None
    assert widget.autoexec.summary.toPlainText() == ""
    assert not widget.autoexec.apply_button.isEnabled()


def test_autoexec_missing_cfg_and_invalid_filename_create_nothing(qtbot, account):
    widget, root, _, video, *_ = account
    widget.autoexec_source.setText(str(video))
    widget.config_file_name.setText("legacy.cfg")
    widget.autoexec.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_autoexec_directory_missing" in widget.autoexec.status.text()
    assert not (root / "game/game").exists()
    widget.config_file_name.setText("../escape.cfg")
    widget.autoexec.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_invalid_filename" in widget.autoexec.status.text()


def test_forged_review_fingerprint_cannot_apply(qtbot, account):
    widget, root, *_ = account
    account_preview(qtbot, widget)
    widget.account_review = replace(widget.account_review, fingerprint="0" * 64)
    widget.account.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_preview_stale" in widget.account.status.text()
    assert not (root / "steam/userdata/42").exists()


def test_apply_error_exposes_recovery_paths_not_exception_contents(qtbot, account, monkeypatch):
    widget, root, *_ = account
    account_preview(qtbot, widget)
    backup, stage = root / ".backup-fixture", root / ".stage-fixture"

    def fail(_):
        raise ConfigApplyError(backup_path=backup, staging_path=stage, rollback_succeeded=False)

    monkeypatch.setattr(module, "apply_account_config", fail)
    widget.account.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_rollback_failed" in widget.account.status.text()
    assert str(backup) in widget.account.status.text()
    assert str(stage) in widget.account.status.text()
    assert "Откат не завершён" in widget.account.status.text()
    assert not widget.account.apply_button.isEnabled()


def test_unexpected_error_string_is_never_rendered(qtbot, account, monkeypatch):
    widget = account[0]

    def fail(*args, **kwargs):
        raise RuntimeError("password=private-secret")

    monkeypatch.setattr(module, "preview_account_config", fail)
    widget.account.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_operation_failed" in widget.account.status.text()
    assert "private-secret" not in visible_output(widget)


def test_worker_keeps_qt_responsive_and_discards_edited_pending_preview(
    qtbot, account, monkeypatch
):
    widget = account[0]
    original = module.preview_account_config
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    ui_thread = threading.get_ident()
    ticks = []

    def delayed(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "preview_account_config", delayed)
    widget.account.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        assert not widget.login.isEnabled()
        assert not widget.open_workspace_button.isEnabled()
        assert not widget.autoexec.preview_button.isEnabled()
        QTimer.singleShot(0, lambda: ticks.append("responsive"))
        qtbot.waitUntil(lambda: bool(ticks))
        widget.login.setText("edited-programmatically")
    finally:
        release.set()
    wait_ready(qtbot, widget)
    assert worker_threads == [worker_threads[0]]
    assert worker_threads[0] != ui_thread
    assert widget.account_review is None
    assert "изменились во время операции" in widget.account.status.text()
    assert not widget.account.apply_button.isEnabled()


def test_close_waits_for_pending_apply_and_blocks_new_actions(qtbot, account, monkeypatch):
    widget, root, *_ = account
    account_preview(qtbot, widget)
    original = module.apply_account_config
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "apply_account_config", delayed)
    widget.account.apply_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        start = time.monotonic()
        closed = widget.bridge.submit(widget.aclose())
        assert time.monotonic() - start < 0.2
        qtbot.waitUntil(lambda: widget._closing)
        assert not closed.done()
        request = widget._request
        widget._preview("account")
        assert widget._request == request
    finally:
        release.set()
    qtbot.waitUntil(closed.done, timeout=10000)
    assert closed.exception() is None
    wait_ready(qtbot, widget)
    assert (root / "steam/userdata/42/730/local/cfg/cs2_video.txt").exists()
    assert not widget.account.preview_button.isEnabled()


def test_standalone_close_stops_owned_bridge(qtbot, qapp):
    qapp.setQuitOnLastWindowClosed(False)
    widget = ConfigFilesWidget()
    qtbot.addWidget(widget)
    widget.show()
    widget.close()
    qtbot.waitUntil(lambda: not widget.bridge._thread.is_alive(), timeout=5000)
    assert widget._closed
    assert widget.bridge.shutdown_error is None


def test_shared_close_does_not_stop_other_panels_bridge(qtbot, panel):
    panel.close()
    qtbot.waitUntil(lambda: panel._closed, timeout=5000)
    assert panel.bridge._thread.is_alive()
