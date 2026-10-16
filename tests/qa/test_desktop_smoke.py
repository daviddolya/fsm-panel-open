import json

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui.smoke import DesktopSmoke
from fsm_panel.ui.window import MainWindow


def test_smoke_checks_packaged_composite_codec():
    from fsm_panel.ui.smoke import verify_item_composite

    verify_item_composite()


def test_composite_failure_cannot_report_success(qtbot, qapp, tmp_path, monkeypatch):
    qapp.setQuitOnLastWindowClosed(False)
    report = tmp_path / "smoke-report.json"

    def unavailable_codec(*args, **kwargs):
        raise OSError("synthetic codec unavailable")

    monkeypatch.setattr("fsm_panel.ui.smoke.compose_item_images", unavailable_codec)
    with AccountRepository(tmp_path / "smoke.sqlite") as repository:
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        smoke = DesktopSmoke(window, report)
        try:
            window.show()
            qtbot.waitUntil(lambda: window._closed, timeout=10_000)
            result = json.loads(report.read_text(encoding="utf8"))
            assert smoke.passed is False
            assert result["passed"] is False
            assert result["item_composite_verified"] is False
            assert result["code"] == "smoke_failed"
            assert repository.list_accounts() == []
            assert not window.bridge._thread.is_alive()
        finally:
            if not window._closed:
                window.close()
                qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()


def test_smoke_cannot_report_success_when_owned_worker_shutdown_fails(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    report = tmp_path / "smoke-report.json"
    with AccountRepository(tmp_path / "smoke.sqlite") as repository:
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        original_close = window.trades.aclose

        async def failed_cleanup():
            await original_close()
            raise RuntimeError("synthetic worker cleanup failure")

        window.trades.aclose = failed_cleanup
        smoke = DesktopSmoke(window, report)
        try:
            window.show()
            qtbot.waitUntil(lambda: window._closed, timeout=10_000)
            assert window.bridge.shutdown_error == "RuntimeError"
            assert not window.bridge._thread.is_alive()
            assert smoke.passed is False
            assert json.loads(report.read_text(encoding="utf8"))["passed"] is False
        finally:
            if not window._closed:
                window.close()
                qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()
