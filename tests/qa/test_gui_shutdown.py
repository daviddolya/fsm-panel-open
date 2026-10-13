import asyncio
import threading

from fsm_panel.adapters.fakes import FakeGame, MemorySecretStore
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
