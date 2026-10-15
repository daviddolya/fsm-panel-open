import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLineEdit

from fsm_panel.adapters.account_config import (
    ConfigApplyError,
    ConfigError,
    create_config_workspace,
)
from fsm_panel.ui import gsi_config as module
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.gsi_config import GsiConfigPanel

TOKEN = "Synthetic_UI_GSI_token_0123456789-abcdef"
NEXT_TOKEN = "Another_UI_GSI_token_9876543210-uvwxyz"


def wait_ready(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=10000)


def visible_output(widget):
    return widget.summary.toPlainText() + "\n" + widget.status.text()


def tree_bytes(path):
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


@pytest.fixture
def panel(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    owner = SimpleNamespace(bridge=AsyncBridge(), _closing=False)
    widget = GsiConfigPanel(owner)
    widget.resize(900, 650)
    qtbot.addWidget(widget)
    widget.show()
    workspace = create_config_workspace(tmp_path / "offline-copy")
    cfg = workspace.game_root / "game/csgo/cfg"
    cfg.mkdir(parents=True)
    (cfg / "gamestate_integration_1.cfg").write_bytes(b"old indexed config\n")
    (cfg / "gamestate_integration_9.cfg").write_bytes(b"other index\r\n")
    (cfg / "autoexec.cfg").write_bytes(b"untouched autoexec\n")
    source = tmp_path / "token-input.txt"
    source.write_bytes(TOKEN.encode("ascii") + b"\r\n")
    yield widget, workspace, cfg, source
    owner._closing = True
    owner.bridge.request_shutdown(widget.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=10000)
    assert owner.bridge.shutdown_error is None


def select(panel):
    widget, workspace, _, source = panel
    widget.workspace_path.setText(str(workspace.root))
    widget.token_source.setText(str(source))
    return widget


def reviewed(qtbot, panel):
    widget = select(panel)
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert widget.preview is not None
    return widget


def test_initial_controls_require_explicit_sources_and_show_offline_limits(panel):
    widget = panel[0]
    assert widget.workspace_path.text() == ""
    assert widget.token_source.text() == ""
    assert widget.preview is None
    assert not widget.preview_button.isEnabled()
    assert not widget.apply_button.isEnabled()
    assert widget.running_on_port.minimum() == 1
    assert widget.running_on_port.maximum() == 65535
    assert widget.running_on_port.value() == 6969
    assert widget.gsi_file_index.minimum() == 1
    assert widget.gsi_file_index.maximum() == 10
    assert widget.encoding.currentText() == "utf-8"
    assert "Порт не проверяется, сервер не запускается" in widget.notice.text()
    assert "Конфиги игры" in widget.notice.text()
    # Apart from editable codec/spinbox internals, only paths accept free text.
    assert set(widget.findChildren(QLineEdit)) == {
        widget.workspace_path,
        widget.token_source,
        widget.encoding.lineEdit(),
        widget.running_on_port.findChild(QLineEdit),
        widget.gsi_file_index.findChild(QLineEdit),
    }


def test_preview_apply_backup_and_equal_rewrite_never_display_token(qtbot, panel):
    widget, workspace, cfg, source = panel
    before, token_before = tree_bytes(cfg), source.read_bytes()
    reviewed(qtbot, panel)
    assert tree_bytes(cfg) == before
    output = visible_output(widget)
    assert "gamestate_integration_1.cfg" in output
    assert "Записей: 1; удалений: 0" in output
    assert widget.fingerprint in output
    assert "Observer Players" not in output
    assert TOKEN not in output
    assert str(source) not in output
    assert widget.apply_button.isEnabled()
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    backups = list(workspace.root.glob(".config-backup-*"))
    assert len(backups) == 1
    assert tree_bytes(backups[0]) == before
    assert str(backups[0]) in widget.status.text()
    assert b"http://127.0.0.1:6969" in (cfg / "gamestate_integration_1.cfg").read_bytes()
    assert TOKEN.encode("ascii") in (cfg / "gamestate_integration_1.cfg").read_bytes()
    assert (cfg / "gamestate_integration_9.cfg").read_bytes() == before[
        "gamestate_integration_9.cfg"
    ]
    assert source.read_bytes() == token_before
    assert widget.preview is None
    assert widget.summary.toPlainText() == ""
    assert not widget.apply_button.isEnabled()
    assert TOKEN not in visible_output(widget)
    reviewed(qtbot, panel)
    assert widget.apply_button.isEnabled()  # Identical content is still rewritten.
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert len(list(workspace.root.glob(".config-backup-*"))) == 2


@pytest.mark.parametrize(
    "field", ["workspace_path", "token_source", "running_on_port", "gsi_file_index", "encoding"]
)
def test_input_edit_invalidates_review_even_when_changed_back(qtbot, panel, field):
    widget = reviewed(qtbot, panel)
    original = widget._selection()
    control = getattr(widget, field)
    if field == "encoding":
        control.setCurrentText("cp1251")
        control.setCurrentText("utf-8")
    elif field in ("running_on_port", "gsi_file_index"):
        value = control.value()
        control.setValue(value + 1)
        control.setValue(value)
    else:
        value = control.text()
        control.setText(value + "-edited")
        control.setText(value)
    assert widget._selection() == original
    assert widget.preview is None
    assert widget.fingerprint is None
    assert widget.summary.toPlainText() == ""
    assert not widget.apply_button.isEnabled()
    request = widget._request
    widget._apply()
    assert widget._request == request


@pytest.mark.parametrize("changed", ["source", "target", "other", "marker"])
def test_external_change_rejects_apply_and_preserves_files(qtbot, panel, changed):
    widget, workspace, cfg, source = panel
    reviewed(qtbot, panel)
    target = {
        "source": source,
        "target": cfg / "gamestate_integration_1.cfg",
        "other": cfg / "autoexec.cfg",
        "marker": workspace.root / ".fsm-config-workspace.json",
    }[changed]
    target.write_bytes(NEXT_TOKEN.encode("ascii"))
    before = tree_bytes(workspace.root)
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_preview_stale" in widget.status.text()
    assert tree_bytes(workspace.root) == before
    assert widget.preview is None
    assert not widget.apply_button.isEnabled()
    assert TOKEN not in visible_output(widget)
    assert NEXT_TOKEN not in visible_output(widget)


def test_unmarked_workspace_and_inside_source_are_rejected(qtbot, panel, tmp_path):
    widget, workspace, cfg, source = panel
    select(panel)
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    widget.workspace_path.setText(str(unmarked))
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_workspace_marker_required" in widget.status.text()
    assert list(unmarked.iterdir()) == []
    widget.workspace_path.setText(str(workspace.root))
    inside = cfg / "inside-token"
    inside.write_bytes(source.read_bytes())
    widget.token_source.setText(str(inside))
    before = tree_bytes(workspace.root)
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert "config_source_destination_overlap" in widget.status.text()
    assert tree_bytes(workspace.root) == before


@pytest.mark.parametrize(
    ("error", "public_code"),
    [
        (RuntimeError("private exception " + TOKEN), "config_operation_failed"),
        (OSError("private path " + TOKEN), "config_file_unavailable"),
        (ValueError(TOKEN), "config_invalid_input"),
        (ConfigError(TOKEN), "config_operation_failed"),
        (ConfigError("gsi_token_invalid"), "gsi_token_invalid"),
    ],
)
def test_errors_never_echo_token_or_arbitrary_exception_fields(
    qtbot, panel, monkeypatch, error, public_code
):
    widget = select(panel)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(module, "preview_gsi_config", fail)
    widget.preview_button.click()
    wait_ready(qtbot, widget)
    assert public_code in widget.status.text()
    assert TOKEN not in visible_output(widget)
    assert widget.summary.toPlainText() == ""
    assert not widget.apply_button.isEnabled()


def test_apply_failure_shows_recovery_paths_without_exception_text(qtbot, panel, monkeypatch):
    widget, workspace, cfg, _ = panel
    reviewed(qtbot, panel)
    before = tree_bytes(cfg)
    backup = workspace.root / ".config-backup-synthetic"
    stage = workspace.root / ".config-stage-synthetic"
    error = ConfigApplyError(backup_path=backup, staging_path=stage, rollback_succeeded=False)
    error.args = (TOKEN,)
    error.code = TOKEN

    def fail(*args):
        raise error

    monkeypatch.setattr(module, "apply_gsi_config", fail)
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_rollback_failed" in widget.status.text()
    assert str(backup) in widget.status.text()
    assert str(stage) in widget.status.text()
    assert "Откат не завершён" in widget.status.text()
    assert TOKEN not in visible_output(widget)
    assert tree_bytes(cfg) == before


@pytest.mark.parametrize("changed", ["fingerprint", "preview"])
def test_altered_review_is_not_applied(qtbot, panel, changed):
    widget = reviewed(qtbot, panel)
    before = tree_bytes(panel[2])
    if changed == "fingerprint":
        widget.fingerprint = "0" * 64
    else:
        widget.preview = replace(widget.preview, running_on_port=7777)
    widget.apply_button.click()
    wait_ready(qtbot, widget)
    assert "config_preview_stale" in widget.status.text()
    assert tree_bytes(panel[2]) == before


def test_worker_does_not_block_qt_and_discards_preview_after_temporary_edit(
    qtbot, panel, monkeypatch
):
    widget = select(panel)
    original = module.preview_gsi_config
    entered, release = threading.Event(), threading.Event()
    threads, ticks = [], []
    main_thread = threading.get_ident()

    def delayed(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "preview_gsi_config", delayed)
    widget.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        assert not widget.token_source.isEnabled()
        assert not widget.apply_button.isEnabled()
        QTimer.singleShot(0, lambda: ticks.append("responsive"))
        qtbot.waitUntil(lambda: bool(ticks))
        widget.running_on_port.setValue(7777)
        widget.running_on_port.setValue(6969)
        request = widget._request
        widget._preview()
        widget._apply()
        assert widget._request == request
    finally:
        release.set()
    wait_ready(qtbot, widget)
    assert threads and all(thread != main_thread for thread in threads)
    assert widget.preview is None
    assert "Параметры изменились" in widget.status.text()
    assert not widget.apply_button.isEnabled()


def test_shared_bridge_responses_do_not_replace_review(qtbot, panel):
    widget = reviewed(qtbot, panel)
    proposal = widget.preview
    output = visible_output(widget)
    widget.owner.bridge.completed.emit(
        {"gsi_config_panel": id(widget) + 1, "request": widget._request, "error": TOKEN}
    )
    widget.owner.bridge.completed.emit(
        {"gsi_config_panel": id(widget), "request": widget._request - 1, "error": TOKEN}
    )
    widget.owner.bridge.failed.emit(TOKEN)
    qtbot.wait(10)
    assert widget.preview is proposal
    assert visible_output(widget) == output


@pytest.mark.parametrize("action", ["preview", "apply"])
def test_shared_bridge_shutdown_joins_pending_work_without_blocking_qt(
    qtbot, panel, monkeypatch, action
):
    widget, workspace, cfg, _ = panel
    select(panel)
    if action == "apply":
        reviewed(qtbot, panel)
    name = "preview_gsi_config" if action == "preview" else "apply_gsi_config"
    original = getattr(module, name)
    entered, release = threading.Event(), threading.Event()
    completed, ticks = [], []

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        result = original(*args, **kwargs)
        completed.append(True)
        return result

    monkeypatch.setattr(module, name, delayed)
    getattr(widget, action + "_button").click()
    qtbot.waitUntil(entered.is_set)
    try:
        widget.owner._closing = True
        started = time.monotonic()
        widget.owner.bridge.request_shutdown(widget.aclose())
        assert time.monotonic() - started < 0.2
        qtbot.waitUntil(lambda: widget._closing)
        assert widget.owner.bridge._thread.is_alive()
        QTimer.singleShot(0, lambda: ticks.append("responsive"))
        qtbot.waitUntil(lambda: bool(ticks))
        request = widget._request
        widget._preview()
        widget._apply()
        assert widget._request == request
        assert not completed
    finally:
        release.set()
    qtbot.waitUntil(lambda: not widget.owner.bridge._thread.is_alive(), timeout=10000)
    assert completed
    assert widget.owner.bridge.shutdown_error is None
    assert not widget.preview_button.isEnabled()
    assert not widget.apply_button.isEnabled()
    if action == "apply":
        assert TOKEN.encode("ascii") in (cfg / "gamestate_integration_1.cfg").read_bytes()
        assert len(list(workspace.root.glob(".config-backup-*"))) == 1
    else:
        assert widget.preview is None
        assert not list(workspace.root.glob(".config-backup-*"))
