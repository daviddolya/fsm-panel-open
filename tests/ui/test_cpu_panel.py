from types import SimpleNamespace

import pytest

from fsm_panel.adapters.bes import OwnedBesService
from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.core.settings import SettingsDocument
from fsm_panel.ui.window import MainWindow


@pytest.fixture
def window(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    repo = AccountRepository(tmp_path / "accounts.sqlite")
    coordinator = Coordinator(repo, game=FakeGame(), lease=FakeLease())
    widget = MainWindow(repo, coordinator, MemorySecretStore(), tmp_path, simulate=True)
    widget.show()
    qtbot.waitUntil(lambda: widget.ready)
    yield widget
    for record in repo.owned_processes():
        repo.forget_process(record["token"])
    widget.close()
    qtbot.waitUntil(lambda: widget._closed)
    widget.deleteLater()
    repo.close()


def test_empty_processes_and_simulator_never_enable_live_bes(qtbot, window):
    panel = window.cpu
    assert not panel.limit_button.isEnabled()
    window.add_sample()
    account = window.repository.list_accounts()[0]
    window.repository.remember_process("synthetic", account.id, 321, 10, "C:/test.exe")
    panel.refresh()
    assert panel.target.count() == 1
    assert not panel.limit_button.isEnabled()
    panel._submit("limit")
    assert panel._future is None
    assert not window.cpu_runtime.enabled


def test_cpu_setting_passes_through_without_inversion_or_clamp(window):
    panel = window.cpu
    assert panel.percent.value() == 25
    window.settings = SettingsDocument({"BES_REDUCTION_PERCENT": 37})
    panel.settings_button.click()
    assert panel.percent.value() == 37
    window.settings = SettingsDocument({"BES_REDUCTION_PERCENT": 150})
    panel.settings_button.click()
    assert panel.percent.value() == 37
    assert "от 1 до 99" in panel.result.toPlainText()


def test_selection_and_errors_are_visible_without_claiming_throttle(qtbot, window):
    panel = window.cpu
    panel.stop_button.click()
    assert "Выберите один" in panel.result.toPlainText()
    window.add_sample()
    account = window.repository.list_accounts()[0]
    window.table.selectRow(0)
    calls = []

    async def stop(account_id):
        calls.append(account_id)
        raise RuntimeError("synthetic_secret_never_print")

    window.cpu_runtime.stop = stop
    panel.stop_button.click()
    qtbot.waitUntil(lambda: panel._future is None)
    assert calls == [account.id]
    assert "RuntimeError" in panel.result.toPlainText()
    assert "synthetic_secret" not in panel.result.toPlainText()
    assert "сессии BES аккаунта завершены" not in panel.result.toPlainText()


def test_limit_dispatch_uses_registered_token_and_reports_delivery_only(qtbot, window):
    # Replace the whole endpoint: this tests UI dispatch, never the native driver.
    calls = []

    async def limit(token, percent, executable):
        calls.append((token, percent, executable))
        return SimpleNamespace(state="delivered")

    window.cpu_runtime.limit = limit
    window.simulate = False
    window.add_sample()
    account = window.repository.list_accounts()[0]
    window.repository.remember_process("synthetic", account.id, 321, 10, "C:/test.exe")
    panel = window.cpu
    panel.refresh()
    panel.executable.setText("C:/reviewed/BES.exe")
    panel.percent.setValue(35)
    panel.limit_button.click()
    qtbot.waitUntil(lambda: panel._future is None)
    assert calls == [("synthetic", 35, "C:/reviewed/BES.exe")]
    assert "delivered" in panel.result.toPlainText()
    assert "не подтверждает" in panel.result.toPlainText()


def test_failed_startup_recovery_can_be_retried_before_enabling_commands(
    qtbot, qapp, tmp_path, monkeypatch
):
    qapp.setQuitOnLastWindowClosed(False)
    broken = True

    async def recover(self):
        if broken:
            raise RuntimeError("synthetic_recovery_failed")

    monkeypatch.setattr(OwnedBesService, "recover", recover)
    with AccountRepository() as repo:
        coordinator = Coordinator(repo, game=FakeGame(), lease=FakeLease())
        widget = MainWindow(repo, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        widget.show()
        try:
            qtbot.waitUntil(lambda: widget._startup.done())
            assert widget._startup.exception() is not None
            assert not widget.ready and not widget.start_button.isEnabled()
            assert widget.cpu.recover_button.isEnabled()
            broken = False
            widget.cpu.recover_button.click()
            qtbot.waitUntil(lambda: widget.ready)
            assert widget.start_button.isEnabled()
        finally:
            broken = False
            widget.close()
            qtbot.waitUntil(lambda: widget._closed)
            widget.deleteLater()
