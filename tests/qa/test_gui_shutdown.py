import asyncio
import threading

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, AccountState, Coordinator
from fsm_panel.ui.window import MainWindow


def test_close_during_pending_admission_cleans_and_joins_worker(qtbot, qapp, tmp_path):
    class WaitingLease:
        def __init__(self):
            self.entered = threading.Event()

        async def admit(self, account_id):
            self.entered.set()
            await asyncio.Event().wait()

    qapp.setQuitOnLastWindowClosed(False)
    with AccountRepository(tmp_path / "pending-admission.sqlite") as repository:
        account = repository.add_account("synthetic_shutdown_during_admission")
        lease = WaitingLease()
        game = FakeGame(launch_delay=0)
        coordinator = Coordinator(repository, game=game, lease=lease)
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        try:
            window.show()
            qtbot.waitUntil(lambda: window.ready)
            window.table.selectRow(0)
            window.start_selected()
            qtbot.waitUntil(lease.entered.is_set)
            for _ in range(3):
                window.close()
            qtbot.waitUntil(lambda: window._closed, timeout=3000)
            assert not window.bridge._thread.is_alive()
            assert window.bridge.shutdown_error is None
            assert repository.get(account.id).state == AccountState.STOPPED
            assert game.launch_attempts[account.id] == 0
        finally:
            if not window._closed:
                window.close()
                qtbot.waitUntil(lambda: window._closed, timeout=3000)
            window.deleteLater()


def test_reconcile_last_trade_keeps_its_account_after_other_inventory_lookup(qtbot, qapp, tmp_path):
    qapp.setQuitOnLastWindowClosed(False)
    with AccountRepository(tmp_path / "trade-account-scope.sqlite") as repository:
        repository.add_account("synthetic_trade_owner")
        repository.add_account("synthetic_inventory_other")
        coordinator = Coordinator(repository, game=FakeGame(), lease=FakeLease())
        window = MainWindow(repository, coordinator, MemorySecretStore(), tmp_path, simulate=True)
        try:
            window.show()
            qtbot.waitUntil(lambda: window.ready)
            panel = window.trades
            window.table.selectRow(0)
            panel._submit("send")
            qtbot.waitUntil(lambda: "sent" in panel.result.toPlainText(), timeout=5000)
            operation_id = panel.operation_id
            window.table.selectRow(1)
            panel._submit("inventory")
            qtbot.waitUntil(
                lambda: "Тестовый инвентарь:" in panel.result.toPlainText(), timeout=5000
            )
            panel._submit("reconcile")
            qtbot.waitUntil(lambda: panel._future.done(), timeout=5000)
            result = panel._future.result()
            assert operation_id in result["message"]
            assert "sent" in result["message"]
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._closed, timeout=5000)
            window.deleteLater()
