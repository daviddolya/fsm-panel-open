import asyncio
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QThread, QTimer

from fsm_panel.adapters.fakes import FakeLease
from fsm_panel.core.settings import SettingsStore
from fsm_panel.notifications.images import ItemComposite, ItemCompositeError
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.notifications import NotificationPanel


@pytest.fixture
def case(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    owner = SimpleNamespace(
        bridge=AsyncBridge(),
        _closing=False,
        simulate=True,
        coordinator=SimpleNamespace(lease=FakeLease(allowed=False)),
        settings=SettingsStore.defaults(),
    )
    clients = []

    def forbid_network(*args, **kwargs):
        clients.append(args)
        raise AssertionError("Offline composition must not construct a network client")

    parent = NotificationPanel(owner, client_factory=forbid_network)
    widget = parent.composite
    parent.show()
    files = [tmp_path / name for name in ("one.png", "two.png", "result.png")]
    for path, color in zip(files[:2], ("red", "blue")):
        with Image.new("RGBA", (2, 1), color) as image:
            image.save(path)
    for field, path in zip((widget.first_path, widget.second_path, widget.output_path), files):
        field.setText(str(path))
    state = SimpleNamespace(releases=[], clients=clients)
    yield widget, parent, owner, state, files
    for release in state.releases:
        release.set()
    owner._closing = True
    owner.bridge.request_shutdown(parent.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=5000)
    assert owner.bridge.shutdown_error is None
    assert not clients and not owner.coordinator.lease.calls
    parent.close()
    parent.deleteLater()


def complete(qtbot, widget):
    qtbot.waitUntil(lambda: widget._future is None, timeout=5000)


def blocking_composer(widget, state, *, commit=False):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    state.releases.append(release)

    def compose(first, second, output, *, cancelled):
        entered.set()
        assert release.wait(5)
        finished.set()
        if cancelled.is_set() and not commit:
            raise ItemCompositeError("image_cancelled")
        return ItemComposite(b"synthetic-result", 4, 1)

    widget._composer = compose
    return entered, release, finished


def test_offline_export_in_simulator_without_lease_or_network(case, qtbot):
    widget, parent, owner, state, files = case
    assert widget.compose_button.isEnabled() and not parent.send_button.isEnabled()
    widget.compose_button.click()
    widget._submit()
    complete(qtbot, widget)
    assert "PNG сохранён: 4 × 1" in widget.status.text()
    with Image.open(files[2]) as image:
        assert list(image.get_flattened_data()) == [(255, 0, 0, 255)] * 2 + [(0, 0, 255, 255)] * 2
    assert parent.photo_path.text() == ""  # Saving never queues or selects a send.
    assert not state.clients and not owner.coordinator.lease.calls


def test_existing_output_is_retained_and_success_allows_new_selection(case, qtbot):
    widget, _, _, _, files = case
    files[2].write_bytes(b"existing-private-data")
    widget.compose_button.click()
    complete(qtbot, widget)
    assert "уже существует" in widget.status.text()
    assert files[2].read_bytes() == b"existing-private-data"
    assert widget.compose_button.isEnabled()


def test_work_runs_off_ui_thread_and_qt_and_bridge_remain_responsive(case, qtbot, qapp):
    widget, _, owner, state, _ = case
    entered, release, _ = blocking_composer(widget, state)
    callback_threads = []
    widget.operation_finished.connect(
        lambda response: callback_threads.append(QThread.currentThread())
    )
    widget.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    pulse = owner.bridge.submit(asyncio.sleep(0, result="pulse"))
    qtbot.waitUntil(lambda: bool(ticks) and pulse.done())
    assert not widget.compose_button.isEnabled()
    release.set()
    complete(qtbot, widget)
    assert callback_threads == [qapp.thread()]


def test_cancel_before_start_cannot_enter_composer(case, qtbot):
    widget, _, owner, state, files = case
    entered, release = threading.Event(), threading.Event()
    state.releases.append(release)

    def hold_loop():
        entered.set()
        assert release.wait(5)

    owner.bridge.loop.call_soon_threadsafe(hold_loop)
    qtbot.waitUntil(entered.is_set)
    widget.compose_button.click()
    widget.cancel_button.click()
    release.set()
    complete(qtbot, widget)
    assert not files[2].exists() and "отменено" in widget.status.text()


@pytest.mark.parametrize("commit", [False, True])
def test_cancel_waits_for_active_work_and_reports_actual_outcome(case, qtbot, commit):
    widget, _, _, state, _ = case
    entered, release, finished = blocking_composer(widget, state, commit=commit)
    widget.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    job = widget._job
    widget.cancel_button.click()
    widget._cancel()
    assert job.cancelled.is_set() and widget._future is not None
    assert not widget.compose_button.isEnabled() and not finished.is_set()
    release.set()
    complete(qtbot, widget)
    assert ("сохранён" if commit else "отменено") in widget.status.text()


def test_external_future_cancel_does_not_release_writer_ownership(case, qtbot):
    widget, parent, owner, state, _ = case
    entered, release, finished = blocking_composer(widget, state, commit=True)
    widget.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    job = widget._job
    assert widget._future.cancel()
    closed = owner.bridge.submit(parent.aclose())
    pulse = owner.bridge.submit(asyncio.sleep(0, result=None))
    qtbot.waitUntil(pulse.done)
    assert not closed.done() and not finished.is_set() and widget._job is job
    assert not widget.compose_button.isEnabled()
    release.set()
    qtbot.waitUntil(closed.done)
    assert closed.result() is None and finished.is_set() and job.finished.done()
    complete(qtbot, widget)
    assert "сохранён" in widget.status.text()


def test_external_cancel_before_coroutine_start_completes_without_work(case, qtbot):
    widget, parent, owner, state, files = case
    entered, release = threading.Event(), threading.Event()
    state.releases.append(release)

    def hold_loop():
        entered.set()
        assert release.wait(5)

    owner.bridge.loop.call_soon_threadsafe(hold_loop)
    qtbot.waitUntil(entered.is_set)
    widget.compose_button.click()
    job = widget._job
    widget._future.cancel()
    closed = owner.bridge.submit(parent.aclose())
    release.set()
    qtbot.waitUntil(closed.done)
    assert closed.result() is None and job.finished.done()
    complete(qtbot, widget)
    assert not files[2].exists() and "отменено" in widget.status.text()


def test_owned_composer_cancellation_produces_explicit_outcome(case, qtbot):
    widget, _, _, _, _ = case

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    widget._composer = cancel
    widget.compose_button.click()
    complete(qtbot, widget)
    assert "отменено" in widget.status.text()


def test_close_keeps_job_reference_across_completion_signal(case, qtbot):
    widget, _, owner, state, _ = case
    entered, release, finished = blocking_composer(widget, state)
    widget.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    job = widget._job
    original_set = job.cancelled.set

    def completion_interleaves():
        original_set()
        widget._job = None

    job.cancelled.set = completion_interleaves
    closing = owner.bridge.submit(widget.aclose())
    pulse = owner.bridge.submit(asyncio.sleep(0, result=True))
    qtbot.waitUntil(pulse.done)
    assert not closing.done()
    release.set()
    qtbot.waitUntil(closing.done)
    assert closing.result() is None and finished.is_set() and job.finished.done()


def test_cancelled_parent_close_waiter_still_joins_file_worker(case, qtbot):
    widget, parent, owner, state, _ = case
    entered, release, finished = blocking_composer(widget, state)
    widget.compose_button.click()
    qtbot.waitUntil(entered.is_set)
    wait = SimpleNamespace(task=None, cancelled=False)

    async def close():
        wait.task = asyncio.create_task(parent.aclose())
        try:
            await wait.task
        except asyncio.CancelledError:
            wait.cancelled = True

    closing = owner.bridge.submit(close())
    qtbot.waitUntil(lambda: wait.task is not None)
    owner.bridge.loop.call_soon_threadsafe(wait.task.cancel)
    pulse = owner.bridge.submit(asyncio.sleep(0, result=None))
    qtbot.waitUntil(pulse.done)
    assert not closing.done() and not finished.is_set()
    release.set()
    qtbot.waitUntil(closing.done)
    assert closing.result() is None and finished.is_set() and wait.cancelled


@pytest.mark.parametrize(
    "error",
    [RuntimeError("private-path-image-data"), ItemCompositeError("private-path-image-data")],
)
def test_decoder_failure_is_private_and_other_results_are_ignored(case, qtbot, caplog, error):
    widget, _, _, _, _ = case

    def fail(*args, **kwargs):
        raise error

    widget._composer = fail
    widget.compose_button.click()
    complete(qtbot, widget)
    before = widget.status.text()
    widget._completed({"item_composite_panel": id(widget), "request": 999, "size": (1, 1)})
    widget._failed("private-path-image-data")
    assert widget.status.text() == before
    assert "private-path-image-data" not in before + caplog.text
    assert widget.compose_button.isEnabled()
