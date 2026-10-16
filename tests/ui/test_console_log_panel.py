import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread, QTimer

from fsm_panel.adapters.console_parser import (
    ConsoleObservation,
    ConsoleParseResult,
    ConsoleReadInfo,
)
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui.bridge import AsyncBridge
from fsm_panel.ui.console_logs import ConsoleLogPanel, _summary
from fsm_panel.ui.window import MainWindow


@pytest.fixture
def case(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    owner = SimpleNamespace(bridge=AsyncBridge(), _closing=False)
    panel = ConsoleLogPanel(owner)
    panel.show()
    path = tmp_path / "синтетический.log"
    path.write_text(
        "01/02 03:04:05 CS_App_Lifetime_Gamestats: Recording FriendJoin\n"
        "01/02 03:05:06 [SteamNetSockets] AuthStatus (steamid: 7654321): ready\n",
        encoding="utf8",
    )
    panel.path.setText(str(path))
    panel.year.setValue(2020)
    releases = []
    yield panel, owner, path, releases
    for release in releases:
        release.set()
    owner._closing = True
    owner.bridge.request_shutdown(panel.aclose())
    qtbot.waitUntil(lambda: not owner.bridge._thread.is_alive(), timeout=5000)
    assert owner.bridge.shutdown_error is None
    panel.close()
    panel.deleteLater()


def done(qtbot, panel):
    qtbot.waitUntil(lambda: panel._job is None, timeout=5000)


def blocked(case):
    panel, _, _, releases = case
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    releases.append(release)
    calls = []
    original = panel._reader

    def reader(path, context, *, cancelled):
        calls.append((path, context, QThread.currentThread()))
        entered.set()
        assert release.wait(5)
        result = original(path, context, cancelled=cancelled)
        finished.set()
        return result

    panel._reader = reader
    return entered, release, finished, calls


def test_reads_copy_offline_without_license_or_game_mutation(case, qtbot, capsys):
    panel, owner, path, _ = case
    before = path.read_bytes()
    panel.read_button.click()
    done(qtbot, panel)
    result = panel.result.toPlainText()
    assert "из строки AuthStatus" in result and "Подлинность не проверена" in result
    assert "'7654321'" in result
    assert str(datetime(2020, 1, 2, 3, 4, 5).timestamp()) in result
    assert "Они не применяются" in result
    assert "Состояние игры не проверялось" in panel.status.text()
    assert path.read_bytes() == before
    assert len(list(path.parent.iterdir())) == 1
    assert not capsys.readouterr().out
    assert not hasattr(owner, "coordinator")  # No license or account dependency is needed.


def test_empty_unidentified_log_is_not_reported_as_valid_identity(case, qtbot):
    panel, _, path, _ = case
    path.write_bytes(b"")
    panel.read_button.click()
    done(qtbot, panel)
    assert "резервное значение 1; аккаунт не установлен" in panel.result.toPlainText()
    assert "fallback_identity" in panel.result.toPlainText()
    assert "с замечаниями" in panel.status.text()


def test_missing_file_does_not_reuse_previous_success(case, qtbot):
    panel, _, path, _ = case
    panel.read_button.click()
    done(qtbot, panel)
    panel.path.setText(str(path.with_name("missing.log")))
    panel.read_button.click()
    done(qtbot, panel)
    assert "Файл не прочитан надёжно" in panel.status.text()
    assert "7654321" not in panel.result.toPlainText()
    assert "file_unavailable_or_unsafe" in panel.result.toPlainText()


def test_empty_selection_clears_previous_result(case):
    panel, _, _, _ = case
    panel.result.setPlainText("old private result")
    panel.path.clear()
    panel.read_button.click()
    assert panel.result.toPlainText() == "" and panel._job is None
    assert "Выберите" in panel.status.text()


def test_partial_parse_has_visible_diagnostics(case, qtbot):
    panel, _, path, _ = case
    path.write_text(
        "13/33 23:01:01 CS_App_Lifetime_Gamestats: Recording FriendJoin\n"
        "[SteamNetSockets] Refreshing ping measurements\n",
        encoding="utf8",
    )
    panel.cached_account.setText("cached-synthetic")
    panel.read_button.click()
    done(qtbot, panel)
    text = panel.result.toPlainText()
    assert "Ошибки строк: 1; отсутствующие даты: 1" in text
    assert "line_error" in text and "missing_timestamp" in text
    assert "с замечаниями" in panel.status.text()


def test_read_worker_has_snapshot_inputs_and_does_not_block_qt_or_loop(case, qtbot, qapp):
    panel, owner, path, _ = case
    entered, release, _, calls = blocked(case)
    panel.cached_account.setText("cache-before")
    panel.srt.setChecked(True)
    panel.read_button.click()
    qtbot.waitUntil(entered.is_set)
    assert not panel.path.isEnabled() and not panel.read_button.isEnabled()
    panel._submit()  # Duplicate programmatic request must not start another worker.
    panel.path.setText("different-path")
    panel.year.setValue(2021)
    panel.cached_account.setText("cache-after")
    panel.srt.setChecked(False)
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    pulse = owner.bridge.submit(asyncio.sleep(0, result=True))
    qtbot.waitUntil(lambda: bool(ticks) and pulse.done())
    selected, context, thread = calls[0]
    assert selected == str(path) and thread != qapp.thread()
    assert context.year() == 2020 and context.srt_enabled() is True
    assert context.initial_account == context.tail_account == "cache-before"
    release.set()
    done(qtbot, panel)
    assert len(calls) == 1
    assert str(datetime(2020, 1, 2, 3, 4, 5).timestamp()) in panel.result.toPlainText()


def test_completion_reaches_qt_thread(case, qtbot, qapp):
    panel, _, _, _ = case
    threads = []
    panel.operation_finished.connect(lambda result: threads.append(QThread.currentThread()))
    panel.read_button.click()
    done(qtbot, panel)
    assert threads == [qapp.thread()]


@pytest.mark.parametrize("external", [False, True])
def test_cancel_retains_worker_and_close_waits_for_handle_owner(case, qtbot, external):
    panel, owner, _, _ = case
    entered, release, finished, _ = blocked(case)
    panel.read_button.click()
    qtbot.waitUntil(entered.is_set)
    job = panel._job
    if external:
        assert panel._future.cancel()
    else:
        panel.cancel_button.click()
    closing = owner.bridge.submit(panel.aclose())
    pulse = owner.bridge.submit(asyncio.sleep(0, result=True))
    qtbot.waitUntil(pulse.done)
    assert not closing.done() and not finished.is_set() and panel._job is job
    assert not panel.read_button.isEnabled()
    release.set()
    qtbot.waitUntil(closing.done)
    assert closing.result() is None and job.finished.done() and finished.is_set()
    done(qtbot, panel)
    assert "отменено" in panel.status.text()


@pytest.mark.parametrize("external", [False, True])
def test_cancel_before_task_starts_never_calls_reader(case, qtbot, external):
    panel, owner, _, releases = case
    entered, release = threading.Event(), threading.Event()
    releases.append(release)

    def hold():
        entered.set()
        assert release.wait(5)

    def forbidden(*args, **kwargs):
        pytest.fail("Unstarted read must remain unstarted")

    panel._reader = forbidden
    owner.bridge.loop.call_soon_threadsafe(hold)
    qtbot.waitUntil(entered.is_set)
    panel.read_button.click()
    if external:
        panel._future.cancel()
    else:
        panel.cancel_button.click()
    closing = owner.bridge.submit(panel.aclose())
    release.set()
    qtbot.waitUntil(closing.done)
    assert closing.result() is None
    done(qtbot, panel)
    assert "отменено" in panel.status.text() and panel.result.toPlainText() == ""


def test_exception_text_never_reaches_result_or_bridge(case, qtbot, capsys):
    panel, owner, _, _ = case
    errors = []
    owner.bridge.failed.connect(errors.append)

    def fail(*args, **kwargs):
        raise OSError("PRIVATE-canary-value")

    panel._reader = fail
    panel.read_button.click()
    done(qtbot, panel)
    assert "PRIVATE" not in panel.status.text() + panel.result.toPlainText()
    assert "Не удалось" in panel.status.text()
    assert not errors and not capsys.readouterr().out


def test_reader_itself_cancelling_has_a_complete_cancelled_response(case, qtbot):
    panel, _, _, _ = case

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    panel._reader = cancel
    panel.read_button.click()
    done(qtbot, panel)
    assert "отменено" in panel.status.text() and panel.result.toPlainText() == ""


def test_close_joins_captured_job_when_qt_completion_clears_current_job(case, qtbot):
    panel, owner, _, _ = case
    entered, release, finished, _ = blocked(case)
    panel.read_button.click()
    qtbot.waitUntil(entered.is_set)
    job = panel._job
    original_set = job.cancelled.set

    def completion_interleaves():
        original_set()
        panel._job = None  # Model a Qt completion crossing the shutdown thread.

    job.cancelled.set = completion_interleaves
    closing = owner.bridge.submit(panel.aclose())
    pulse = owner.bridge.submit(asyncio.sleep(0, result=True))
    qtbot.waitUntil(pulse.done)
    assert not closing.done()
    release.set()
    qtbot.waitUntil(closing.done)
    assert closing.result() is None and finished.is_set() and job.finished.done()


def test_display_is_bounded_and_control_characters_are_quoted():
    result = ConsoleParseResult(
        ConsoleObservation("<b>private</b>\n" + "x" * 1_000_000, "\x00\r\n"),
        (),
        "auth",
        ConsoleReadInfo(2_000_000, 1_000_020, 1_000_020, 0),
    )
    text = _summary(result)
    assert len(text) < 2000 and "(сокращено)" in text
    assert "\\n" in text and "\\x00" in text and "\\r" in text
    assert len(text.splitlines()) < 20


def test_main_window_has_readonly_log_tab_and_joins_it_after_other_cleanup_error(
    qtbot, qapp, tmp_path
):
    qapp.setQuitOnLastWindowClosed(False)
    with AccountRepository(tmp_path / "state.sqlite") as repository:
        window = MainWindow(
            repository,
            Coordinator(repository, game=FakeGame(), lease=FakeLease()),
            MemorySecretStore(),
            tmp_path,
            simulate=True,
        )
        joined = []
        close_logs = window.console_logs.aclose
        close_notifications = window.notifications.aclose

        async def join():
            await close_logs()
            joined.append(True)

        async def fail():
            await close_notifications()
            raise RuntimeError("synthetic failure")

        window.console_logs.aclose = join
        window.notifications.aclose = fail
        window.show()
        qtbot.waitUntil(lambda: window.ready)
        assert window.logs_tabs.widget(0) is window.log
        assert window.logs_tabs.widget(1) is window.console_logs
        window.close()
        qtbot.waitUntil(lambda: window._closed, timeout=5000)
        assert joined == [True] and not window.bridge._thread.is_alive()
        assert window.bridge.shutdown_error == "RuntimeError"
        window.deleteLater()
