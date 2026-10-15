"""Independent shared-desktop GSI checks using synthetic offline file trees."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSignalBlocker, Qt, QTimer

from fsm_panel.adapters import account_config as configs
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui import gsi_config as gsi_ui
from fsm_panel.ui.window import MainWindow

TOKEN = "QA_PRIVATE_GSI_UI_TOKEN_0123456789-abcdef"
NEXT_TOKEN = "QA_UPDATED_GSI_UI_TOKEN_9876543210-abcdef"
PRIVATE_ERROR = "QA_PRIVATE_GSI_UI_EXCEPTION"
FILENAME = "gamestate_integration_3.cfg"


def tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def ready(qtbot, panel):
    qtbot.waitUntil(lambda: panel._future is None, timeout=10000)


def diagnostics(window):
    return "\n".join(
        (
            window.statusBar().currentMessage(),
            window.log.toPlainText(),
            window.gsi_config.status.text(),
            window.gsi_config.summary.toPlainText(),
            window.config_files.workspace_status.text(),
            window.config_files.autoexec.status.text(),
            window.config_files.autoexec.summary.toPlainText(),
        )
    )


@pytest.fixture
def inputs(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    repository = AccountRepository(tmp_path / "synthetic.sqlite")
    coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
    window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
    window.show()
    qtbot.waitUntil(lambda: window.ready)
    copy_panel = window.config_files
    root = tmp_path / "isolated"
    copy_panel.workspace_path.setText(str(root))
    copy_panel.create_workspace_button.click()
    ready(qtbot, copy_panel)
    assert copy_panel.workspace is not None
    cfg = root / "game/game/csgo/cfg"
    cfg.mkdir(parents=True)
    (cfg / FILENAME).write_bytes(b"original GSI file\r\n")
    (cfg / "gamestate_integration_1.cfg").write_bytes(b"preserve another GSI index\r\n")
    (cfg / "autoexec.cfg").write_bytes(b"original autoexec\r\n")
    (cfg / "legacy.cfg").write_bytes(b"legacy autoexec\r\n")
    (cfg / "nested").mkdir()
    (cfg / "nested/keep").write_bytes(b"unrelated private data")
    token = tmp_path / "private-token-source"
    token.write_bytes(TOKEN.encode() + b"\r\n")
    autoexec = tmp_path / "autoexec-source"
    autoexec.write_bytes(b"new synthetic autoexec\n")
    copy_panel.autoexec_source.setText(str(autoexec))
    copy_panel.config_file_name.setText("legacy.cfg")
    panel = window.gsi_config
    try:
        yield SimpleNamespace(
            window=window,
            panel=panel,
            copy_panel=copy_panel,
            root=root,
            cfg=cfg,
            token=token,
            autoexec=autoexec,
        )
    finally:
        window.close()
        qtbot.waitUntil(lambda: window._closed, timeout=10000)
        assert not window.bridge._thread.is_alive()
        window.deleteLater()
        repository.close()


def select(inputs):
    panel = inputs.panel
    inputs.window.tabs.setCurrentWidget(panel)
    panel.workspace_path.setText(str(inputs.root))
    panel.token_source.setText(str(inputs.token))
    panel.running_on_port.setValue(65535)
    panel.gsi_file_index.setValue(3)


def review(qtbot, inputs):
    select(inputs)
    inputs.panel.preview_button.click()
    ready(qtbot, inputs.panel)
    assert inputs.panel.preview is not None
    assert inputs.panel.apply_button.isEnabled()


def test_main_window_uses_one_bridge_and_requires_explicit_gsi_workspace(qtbot, inputs, tmp_path):
    window, panel = inputs.window, inputs.panel
    assert panel.owner is window
    assert panel.owner.bridge is inputs.copy_panel.bridge
    assert window.tabs.tabText(window.tabs.indexOf(panel)) == "GSI-конфиг"
    assert panel.workspace_path.text() == "" and panel.token_source.text() == ""
    assert not panel.preview_button.isEnabled() and not panel.apply_button.isEnabled()
    outside = tmp_path / "other-game"
    outside.mkdir()
    (outside / "keep").write_bytes(b"never use global game path for this copy")
    window.settings.set("CSGO_PATH", str(outside))
    before_settings = window.settings.to_dict(include_secrets=True)
    before_outside, before_cfg = tree(outside), tree(inputs.cfg)
    review(qtbot, inputs)
    assert FILENAME in panel.summary.toPlainText()
    assert TOKEN not in diagnostics(window)
    assert str(inputs.token) not in diagnostics(window)
    panel.apply_button.click()
    ready(qtbot, panel)
    assert b"http://127.0.0.1:65535" in (inputs.cfg / FILENAME).read_bytes()
    assert TOKEN.encode() in (inputs.cfg / FILENAME).read_bytes()
    backups = list(inputs.root.glob(".config-backup-*"))
    assert len(backups) == 1 and tree(backups[0]) == before_cfg
    assert tree(outside) == before_outside
    assert window.settings.to_dict(include_secrets=True) == before_settings
    assert inputs.token.read_bytes() == TOKEN.encode() + b"\r\n"
    assert panel.status.textFormat() == Qt.TextFormat.PlainText
    assert TOKEN not in diagnostics(window)


@pytest.mark.parametrize(
    "field", ["workspace_path", "token_source", "running_on_port", "gsi_file_index", "encoding"]
)
def test_selection_recheck_blocks_apply_even_if_edit_signals_were_suppressed(qtbot, inputs, field):
    review(qtbot, inputs)
    panel = inputs.panel
    before = tree(inputs.root)
    control = getattr(panel, field)
    blocker = QSignalBlocker(control)
    if field in ("workspace_path", "token_source"):
        control.setText(control.text() + "changed")
    elif field == "encoding":
        control.setCurrentText("utf-8-sig")
    else:
        control.setValue(control.value() - 1)
    blocker.unblock()
    assert panel.preview is not None  # Deliberately bypass the ordinary edit slot.
    request = panel._request
    panel.apply_button.click()
    assert panel._request == request and panel._future is None
    assert panel.preview is None and panel.fingerprint is None
    assert not panel.apply_button.isEnabled()
    assert tree(inputs.root) == before


def test_other_panel_error_does_not_complete_or_disclose_pending_gsi_preview(
    qtbot, inputs, monkeypatch
):
    select(inputs)
    panel = inputs.panel
    entered, release = threading.Event(), threading.Event()
    original = gsi_ui.preview_gsi_config

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    async def unrelated_failure():
        raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(gsi_ui, "preview_gsi_config", delayed)
    panel.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    pending = panel._future
    try:
        other = inputs.window.bridge.submit(unrelated_failure())
        qtbot.waitUntil(other.done)
        qtbot.waitUntil(lambda: "RuntimeError" in inputs.window.log.toPlainText())
        assert panel._future is pending and not pending.done()
        assert not panel.preview_button.isEnabled() and not panel.apply_button.isEnabled()
        assert PRIVATE_ERROR not in diagnostics(inputs.window)
    finally:
        release.set()
    ready(qtbot, panel)
    assert panel.preview is not None and panel.apply_button.isEnabled()
    assert TOKEN not in diagnostics(inputs.window)


def test_pending_preview_rechecks_selection_even_without_a_generation_signal(
    qtbot, inputs, monkeypatch
):
    select(inputs)
    panel = inputs.panel
    entered, release = threading.Event(), threading.Event()
    original = gsi_ui.preview_gsi_config

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(gsi_ui, "preview_gsi_config", delayed)
    panel.preview_button.click()
    qtbot.waitUntil(entered.is_set)
    generation = panel._generation
    try:
        blocker = QSignalBlocker(panel.gsi_file_index)
        panel.gsi_file_index.setValue(9)
        blocker.unblock()
        assert panel._generation == generation
    finally:
        release.set()
    ready(qtbot, panel)
    assert panel.preview is None and panel.fingerprint is None
    assert not panel.apply_button.isEnabled()
    assert panel.summary.toPlainText() == ""
    assert not list(inputs.root.glob(".config-*"))


@pytest.mark.parametrize("member", ["token", "target"])
def test_same_byte_replacement_invalidates_the_review_displayed_in_main_window(
    qtbot, inputs, member
):
    review(qtbot, inputs)
    selected = inputs.token if member == "token" else inputs.cfg / FILENAME
    state = selected.stat()
    replacement = selected.with_suffix(".replacement")
    replacement.write_bytes(selected.read_bytes())
    os.utime(replacement, ns=(state.st_atime_ns, state.st_mtime_ns))
    os.replace(replacement, selected)
    before = tree(inputs.root)
    inputs.panel.apply_button.click()
    ready(qtbot, inputs.panel)
    assert "config_preview_stale" in inputs.panel.status.text()
    assert tree(inputs.root) == before
    assert inputs.panel.preview is None and not inputs.panel.apply_button.isEnabled()
    assert TOKEN not in diagnostics(inputs.window)


@pytest.mark.parametrize("first", ["gsi", "autoexec"])
def test_actual_apply_on_one_page_makes_the_other_pages_old_review_stale(qtbot, inputs, first):
    review(qtbot, inputs)
    inputs.copy_panel.autoexec.preview_button.click()
    ready(qtbot, inputs.copy_panel)
    assert inputs.copy_panel.autoexec_review is not None
    gsi_actions = (inputs.panel, inputs.panel.apply_button, inputs.panel.status)
    auto_actions = (
        inputs.copy_panel,
        inputs.copy_panel.autoexec.apply_button,
        inputs.copy_panel.autoexec.status,
    )
    winner, stale = (gsi_actions, auto_actions) if first == "gsi" else (auto_actions, gsi_actions)
    winner[1].click()
    ready(qtbot, winner[0])
    after_winner = tree(inputs.root)
    stale[1].click()
    ready(qtbot, stale[0])
    assert "config_preview_stale" in stale[2].text()
    assert tree(inputs.root) == after_winner
    assert not stale[1].isEnabled()
    assert TOKEN not in diagnostics(inputs.window)


@pytest.mark.parametrize("rollback_denied", [False, True])
@pytest.mark.parametrize("stage_cleanup_denied", [False, True])
def test_real_install_and_cleanup_failures_display_only_existing_recovery_paths(
    qtbot, inputs, monkeypatch, rollback_denied, stage_cleanup_denied
):
    review(qtbot, inputs)
    before = tree(inputs.cfg)
    original_replace, original_remove = configs._replace_bounded, configs.shutil.rmtree

    def fail_install(source, destination, workspace):
        if source.name.startswith(".config-stage-"):
            raise OSError(PRIVATE_ERROR)
        if rollback_denied and source.name.startswith(".config-backup-"):
            raise OSError(TOKEN)
        return original_replace(source, destination, workspace)

    def fail_cleanup(path, *args, **kwargs):
        if stage_cleanup_denied and Path(path).name.startswith(".config-stage-"):
            raise OSError(NEXT_TOKEN)
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(configs, "_replace_bounded", fail_install)
    monkeypatch.setattr(configs.shutil, "rmtree", fail_cleanup)
    inputs.panel.apply_button.click()
    ready(qtbot, inputs.panel)
    message = inputs.panel.status.text()
    backups = list(inputs.root.glob(".config-backup-*"))
    stages = list(inputs.root.glob(".config-stage-*"))
    if rollback_denied:
        assert "config_rollback_failed" in message
        assert len(backups) == 1 and str(backups[0]) in message
        assert tree(backups[0]) == before
        assert not inputs.cfg.exists()
    else:
        assert "config_apply_failed" in message and "Исходная копия восстановлена" in message
        assert tree(inputs.cfg) == before and backups == []
    if stage_cleanup_denied:
        assert len(stages) == 1 and str(stages[0]) in message
        assert TOKEN.encode() in (stages[0] / FILENAME).read_bytes()
    else:
        assert stages == []
    assert "GSI сохранён" not in message
    assert inputs.panel.preview is None and not inputs.panel.apply_button.isEnabled()
    for private in (TOKEN, NEXT_TOKEN, PRIVATE_ERROR):
        assert private not in diagnostics(inputs.window)


@pytest.mark.parametrize("earlier_cleanup_fails", [False, True])
@pytest.mark.parametrize("source_changes", [False, True])
def test_window_shutdown_waits_for_real_gsi_staging_despite_earlier_cleanup_failure(
    qtbot, inputs, monkeypatch, earlier_cleanup_fails, source_changes
):
    review(qtbot, inputs)
    window, panel = inputs.window, inputs.panel
    before = tree(inputs.cfg)
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    original = configs.shutil.copytree

    def delayed_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).parent == inputs.root:
            worker_threads.append(threading.current_thread())
            entered.set()
            assert release.wait(5)
        return result

    async def fail_earlier_cleanup():
        raise RuntimeError(PRIVATE_ERROR)

    if earlier_cleanup_fails:
        monkeypatch.setattr(inputs.copy_panel, "aclose", fail_earlier_cleanup)
    monkeypatch.setattr(configs.shutil, "copytree", delayed_copy)
    panel.apply_button.click()
    qtbot.waitUntil(entered.is_set)
    pending = panel._future
    try:
        window.close()
        window.close()
        qtbot.waitUntil(lambda: panel._closing)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
        assert not window._closed and not pending.done()
        assert window.bridge._thread.is_alive()
        assert worker_threads[0].ident != threading.get_ident()
        assert tree(inputs.cfg) == before
        request = panel._request
        panel._preview()
        panel._apply()
        assert panel._request == request
        if source_changes:
            inputs.token.write_bytes(NEXT_TOKEN.encode())
    finally:
        release.set()
    qtbot.waitUntil(lambda: window._closed, timeout=10000)
    qtbot.waitUntil(lambda: not worker_threads[0].is_alive(), timeout=10000)
    assert pending.done() and not pending.cancelled() and pending.exception() is None
    assert not window.bridge._thread.is_alive()
    assert window.bridge.shutdown_error == ("RuntimeError" if earlier_cleanup_fails else None)
    backups = list(inputs.root.glob(".config-backup-*"))
    if source_changes:
        assert tree(inputs.cfg) == before and backups == []
        assert "config_preview_stale" in panel.status.text()
    else:
        assert len(backups) == 1 and tree(backups[0]) == before
        assert TOKEN.encode() in (inputs.cfg / FILENAME).read_bytes()
    assert not list(inputs.root.glob(".config-stage-*"))
    for private in (TOKEN, NEXT_TOKEN, PRIVATE_ERROR):
        assert private not in diagnostics(window)


def test_close_with_both_cfg_writers_pending_waits_for_winner_and_stale_competitor(
    qtbot, inputs, monkeypatch
):
    review(qtbot, inputs)
    inputs.copy_panel.autoexec.preview_button.click()
    ready(qtbot, inputs.copy_panel)
    before = tree(inputs.cfg)
    entered, release = threading.Event(), threading.Event()
    original = configs.shutil.copytree

    def delayed_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).parent == inputs.root:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(configs.shutil, "copytree", delayed_copy)
    inputs.panel.apply_button.click()
    qtbot.waitUntil(entered.is_set)
    inputs.copy_panel.autoexec.apply_button.click()
    pending_gsi, pending_autoexec = inputs.panel._future, inputs.copy_panel._future
    try:
        assert pending_gsi is not None and pending_autoexec is not None
        inputs.window.close()
        qtbot.waitUntil(lambda: inputs.copy_panel._closing)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
        assert not pending_autoexec.done() and not pending_gsi.done()
        assert not inputs.window._closed
        assert tree(inputs.cfg) == before
    finally:
        release.set()
    qtbot.waitUntil(lambda: inputs.window._closed, timeout=10000)
    assert pending_gsi.exception() is None and pending_autoexec.exception() is None
    assert inputs.window.bridge.shutdown_error is None
    assert "config_preview_stale" in inputs.copy_panel.autoexec.status.text()
    backups = list(inputs.root.glob(".config-backup-*"))
    assert len(backups) == 1 and tree(backups[0]) == before
    assert (inputs.cfg / "autoexec.cfg").read_bytes() == before["autoexec.cfg"]
    assert (inputs.cfg / "legacy.cfg").read_bytes() == before["legacy.cfg"]
    assert TOKEN.encode() in (inputs.cfg / FILENAME).read_bytes()
    assert TOKEN not in diagnostics(inputs.window)
