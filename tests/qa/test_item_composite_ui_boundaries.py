"""Independent Qt checks for offline composition ownership and manual-only delivery."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QFileDialog

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.core.settings import SettingsStore
from fsm_panel.notifications.images import compose_item_image_files
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.notifications import NotificationPanel
from fsm_panel.ui.window import MainWindow

from .test_item_composite_boundaries import FIRST, SECOND

PRIVATE = "private image filename and arbitrary decoder diagnostics"


@pytest.fixture
def panel(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    state = SimpleNamespace(clients=[], releases=[])

    def forbidden_client(*args, **kwargs):
        state.clients.append(True)
        raise AssertionError("offline composition constructed a Telegram client")

    owner = SimpleNamespace(
        bridge=AsyncBridge(),
        coordinator=SimpleNamespace(lease=FakeLease(allowed=False)),
        settings=SettingsStore.defaults(),
        simulate=True,
        _closing=False,
    )
    widget = NotificationPanel(owner, client_factory=forbidden_client)
    first, second, output = (tmp_path / name for name in ("first.png", "second.png", "result.png"))
    first.write_bytes(FIRST)
    second.write_bytes(SECOND)
    state.paths = (first, second, output)
    for control, path in zip(
        (widget.composite.first_path, widget.composite.second_path, widget.composite.output_path),
        state.paths,
        strict=True,
    ):
        control.setText(str(path))
    widget.show()
    try:
        yield widget, owner, state
    finally:
        for release in state.releases:
            release.set()
        owner._closing = True
        owner.bridge.request_shutdown(widget.aclose())
        qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=10000)
        assert owner.bridge.shutdown_error is None
        widget.close()
        widget.deleteLater()


def _complete(qtbot, composite):
    qtbot.waitUntil(lambda: composite._future is None, timeout=5000)


def _loop_turn(qtbot, loop):
    reached = threading.Event()
    loop.call_soon_threadsafe(lambda: loop.call_soon(reached.set))
    qtbot.waitUntil(reached.is_set, timeout=3000)


def _blocked_composer(state, *, ignore_cancel=False):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    state.releases.append(release)

    def compose(first, second, output, *, cancelled):
        entered.set()
        assert release.wait(5), "test did not release its synthetic worker"
        try:
            return compose_item_image_files(
                first, second, output, cancelled=None if ignore_cancel else cancelled
            )
        finally:
            finished.set()

    return compose, entered, release, finished


def test_explicit_local_composition_needs_no_lease_and_cannot_change_manual_send(panel, qtbot):
    widget, owner, state = panel
    composite = widget.composite
    widget.photo_path.setText("an earlier manually chosen photo")
    widget.message.setPlainText("an earlier manually written caption")
    before = owner.settings.to_dict(include_secrets=True)
    assert composite.owner is owner and composite.thread() is QThread.currentThread()
    assert composite.compose_button.isEnabled() and not widget.send_button.isEnabled()
    composite.compose_button.click()
    _complete(qtbot, composite)
    assert state.paths[2].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "2 × 1" in composite.status.text()
    assert widget.photo_path.text() == "an earlier manually chosen photo"
    assert widget.message.toPlainText() == "an earlier manually written caption"
    assert owner.settings.to_dict(include_secrets=True) == before
    assert not owner.coordinator.lease.calls and not state.clients
    assert composite.status.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize("index", [0, 1, 2])
def test_cancelled_file_chooser_preserves_the_operator_selection(panel, monkeypatch, index):
    widget, _, _ = panel
    composite = widget.composite
    controls = (composite.first_path, composite.second_path, composite.output_path)
    before = tuple(control.text() for control in controls)
    called = []

    def chooser(*args, **kwargs):
        called.append(True)
        return "", ""

    name = "getSaveFileName" if index == 2 else "getOpenFileName"
    monkeypatch.setattr(QFileDialog, name, chooser)
    composite.browse_buttons[index].click()
    assert called == [True]
    assert tuple(control.text() for control in controls) == before
    assert composite._future is None


def test_cancel_before_worker_start_creates_no_file_and_never_admits(panel, qtbot):
    widget, owner, state = panel
    composite = widget.composite
    entered, release = threading.Event(), threading.Event()
    state.releases.append(release)

    def occupy_loop():
        entered.set()
        assert release.wait(5)

    owner.bridge.loop.call_soon_threadsafe(occupy_loop)
    qtbot.waitUntil(entered.is_set)
    composite.compose_button.click()
    active = composite._future
    composite.cancel_button.click()
    assert not active.done() and not composite.compose_button.isEnabled()
    release.set()
    _complete(qtbot, composite)
    assert active.result()["error"] == "cancelled"
    assert not state.paths[2].exists()
    assert not owner.coordinator.lease.calls and not state.clients


def test_cancel_during_bounded_work_retains_owner_and_keeps_qt_responsive(panel, qtbot):
    widget, _, state = panel
    composite = widget.composite
    composite._composer, entered, release, finished = _blocked_composer(state)
    composite.compose_button.click()
    active, job = composite._future, composite._job
    qtbot.waitUntil(entered.is_set)
    composite.cancel_button.click()
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    qtbot.waitUntil(lambda: bool(ticks))
    assert composite._future is active and composite._job is job
    assert job.cancelled.is_set() and not finished.is_set() and not active.done()
    assert not composite.compose_button.isEnabled() and not composite.cancel_button.isEnabled()
    composite._submit()
    assert composite._future is active
    release.set()
    _complete(qtbot, composite)
    assert finished.is_set() and not state.paths[2].exists()
    assert active.result()["error"] == "cancelled"
    assert composite.compose_button.isEnabled()


def test_late_cancel_waits_for_admitted_write_and_displays_success(panel, qtbot, monkeypatch):
    widget, _, state = panel
    composite = widget.composite
    entered, release = threading.Event(), threading.Event()
    state.releases.append(release)
    original_open = Path.open

    def held_output(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == state.paths[2] and mode == "xb":
            entered.set()
            if not release.wait(5):
                handle.close()
                raise AssertionError("test did not release the admitted write")
        return handle

    monkeypatch.setattr(Path, "open", held_output)
    composite.compose_button.click()
    active = composite._future
    qtbot.waitUntil(entered.is_set)
    composite.cancel_button.click()
    assert not active.done() and composite._future is active
    release.set()
    _complete(qtbot, composite)
    assert "error" not in active.result()
    assert "2 × 1" in composite.status.text()
    assert state.paths[2].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_stale_completion_and_foreign_failure_cannot_release_current_work(panel, qtbot):
    widget, _, state = panel
    composite = widget.composite
    composite.compose_button.click()
    previous = composite._future
    _complete(qtbot, composite)
    state.paths[2].unlink()
    composite._composer, entered, release, finished = _blocked_composer(state)
    composite.compose_button.click()
    active, job = composite._future, composite._job
    qtbot.waitUntil(entered.is_set)
    composite._completed(previous.result())
    composite._failed(PRIVATE)
    assert composite._future is active and composite._job is job
    assert not composite.compose_button.isEnabled() and not finished.is_set()
    assert PRIVATE not in composite.status.text()
    composite.cancel_button.click()
    release.set()
    _complete(qtbot, composite)


def test_arbitrary_decoder_error_is_private_and_allows_corrected_selection(panel, qtbot, caplog):
    widget, _, state = panel
    composite = widget.composite

    def broken(*args, **kwargs):
        raise OSError(PRIVATE)

    composite._composer = broken
    composite.compose_button.click()
    pending = composite._future
    _complete(qtbot, composite)
    assert PRIVATE not in composite.status.text() + caplog.text + repr(pending.result())
    assert pending.result()["error"] == "failed" and not state.paths[2].exists()
    assert composite.compose_button.isEnabled()
    composite._composer = compose_item_image_files
    composite.compose_button.click()
    _complete(qtbot, composite)
    assert state.paths[2].exists()


def test_repeated_cancellation_of_close_cannot_outlive_owned_worker(panel, qtbot):
    widget, owner, state = panel
    composite = widget.composite
    composite._composer, entered, release, finished = _blocked_composer(state)
    composite.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    closing, closed = threading.Event(), threading.Event()
    tasks = []

    async def close():
        tasks.append(asyncio.current_task())
        closing.set()
        try:
            await widget.aclose()
        finally:
            closed.set()

    close_future = asyncio.run_coroutine_threadsafe(close(), owner.bridge.loop)
    qtbot.waitUntil(closing.is_set)
    for _ in range(3):
        owner.bridge.loop.call_soon_threadsafe(tasks[0].cancel)
        _loop_turn(qtbot, owner.bridge.loop)
        assert not closed.is_set() and not close_future.done() and not finished.is_set()
    release.set()
    qtbot.waitUntil(closed.is_set)
    assert close_future.cancelled() and finished.is_set()
    assert not state.paths[2].exists()


def test_cancelled_bridge_future_cannot_discard_an_already_admitted_writer(
    panel, qtbot, monkeypatch
):
    widget, owner, state = panel
    composite = widget.composite
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    state.releases.append(release)
    original_open = Path.open

    def held_output(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == state.paths[2] and mode == "xb":
            entered.set()
            if not release.wait(5):
                handle.close()
                raise AssertionError("test did not release the admitted writer")
        return handle

    async def close():
        try:
            await widget.aclose()
        finally:
            closed.set()

    monkeypatch.setattr(Path, "open", held_output)
    composite.compose_button.click()
    caller_future = composite._future
    qtbot.waitUntil(entered.is_set)
    assert caller_future.cancel()
    _loop_turn(qtbot, owner.bridge.loop)
    cleanup = asyncio.run_coroutine_threadsafe(close(), owner.bridge.loop)
    _loop_turn(qtbot, owner.bridge.loop)
    assert not closed.is_set() and not cleanup.done()
    assert not composite.compose_button.isEnabled()
    release.set()
    qtbot.waitUntil(closed.is_set)
    assert cleanup.result(timeout=1) is None
    assert state.paths[2].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_cancelled_bridge_future_before_start_cannot_leave_unfinishable_ownership(panel, qtbot):
    widget, owner, state = panel
    composite = widget.composite
    entered, release = threading.Event(), threading.Event()
    state.releases.append(release)
    calls = []

    def compose(*args, **kwargs):
        calls.append(kwargs["cancelled"].is_set())
        return compose_item_image_files(*args, **kwargs)

    def occupy_loop():
        entered.set()
        assert release.wait(5)

    composite._composer = compose
    owner.bridge.loop.call_soon_threadsafe(occupy_loop)
    qtbot.waitUntil(entered.is_set)
    composite.compose_button.click()
    assert composite._future.cancel()
    release.set()
    _loop_turn(qtbot, owner.bridge.loop)
    cleanup = asyncio.run_coroutine_threadsafe(widget.aclose(), owner.bridge.loop)
    qtbot.waitUntil(cleanup.done, timeout=3000)
    assert cleanup.result(timeout=1) is None
    assert all(calls) and not state.paths[2].exists()


def test_mainwindow_close_joins_composition_after_another_panel_cleanup_failure(
    qtbot, qapp, tmp_path
):
    qapp.setQuitOnLastWindowClosed(False)
    repository = AccountRepository(tmp_path / "synthetic.sqlite")
    coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
    window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
    state = SimpleNamespace(releases=[])
    composite = window.notifications.composite
    composite._composer, entered, release, finished = _blocked_composer(state)
    first, second, output = (tmp_path / name for name in ("a.png", "b.png", "out.png"))
    first.write_bytes(FIRST)
    second.write_bytes(SECOND)
    for control, path in zip(
        (composite.first_path, composite.second_path, composite.output_path),
        (first, second, output),
        strict=True,
    ):
        control.setText(str(path))

    async def broken_trade_close():
        raise RuntimeError(PRIVATE)

    window.trades.aclose = broken_trade_close
    try:
        window.show()
        qtbot.waitUntil(lambda: window.ready)
        composite.compose_button.click()
        qtbot.waitUntil(entered.is_set)
        window.close()
        _loop_turn(qtbot, window.bridge.loop)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: bool(ticks))
        assert window._closing and not window._closed
        assert window.bridge._thread.is_alive() and not finished.is_set()
        window.close()
        release.set()
        qtbot.waitUntil(lambda: window._closed, timeout=5000)
        assert finished.is_set() and not window.bridge._thread.is_alive()
        assert window.bridge.shutdown_error == "RuntimeError"
        assert not output.exists() and PRIVATE not in window.log.toPlainText()
    finally:
        release.set()
        if not window._closed:
            window.close()
            qtbot.waitUntil(lambda: window._closed, timeout=10000)
        window.deleteLater()
        repository.close()
