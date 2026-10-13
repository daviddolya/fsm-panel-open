"""Real local disposable Python children, never Steam or another user's process."""

import sys

import psutil
import pytest

from fsm_panel.adapters.processes import OwnedProcessController
from fsm_panel.core import AccountRepository


@pytest.mark.asyncio
async def test_real_child_is_owned_stopped_and_removed_from_journal(tmp_path):
    with AccountRepository(tmp_path / "owned.sqlite") as repo:
        account = repo.add_account("synthetic_local_process")
        controller = OwnedProcessController(repo)
        owned = await controller.launch(
            account.id, [sys.executable, "-c", "import time; time.sleep(15)"]
        )
        try:
            assert psutil.Process(owned.pid).create_time() == owned.create_time
            assert len(repo.owned_processes()) == 1
            await controller.stop(account.id)
            assert not psutil.pid_exists(owned.pid)
            assert not repo.owned_processes()
        finally:
            await controller.stop(account.id)


@pytest.mark.asyncio
async def test_real_child_recovered_by_new_repository_instance(tmp_path):
    path = tmp_path / "recovery.sqlite"
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic_recovery_process")
        controller = OwnedProcessController(repo)
        owned = await controller.launch(
            account.id, [sys.executable, "-c", "import time; time.sleep(15)"]
        )
    with AccountRepository(path) as reopened:
        recovery = OwnedProcessController(reopened)
        try:
            assert reopened.owned_processes()[0]["pid"] == owned.pid
            await recovery.recover()
            assert not psutil.pid_exists(owned.pid)
            assert not reopened.owned_processes()
        finally:
            await recovery.recover()
