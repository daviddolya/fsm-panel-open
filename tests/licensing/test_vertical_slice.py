"""Real service + signed client + durable application core, synthetic game only."""

import asyncio

import httpx
import pytest

from fsm_panel.adapters.fakes import FakeGame, MemorySecretStore
from fsm_panel.application import LicenseAdmission
from fsm_panel.core import AccountRepository, AccountState, Coordinator, import_accounts
from fsm_panel.licensing import LicenseUnavailable

from .test_client import make_client


@pytest.mark.asyncio
async def test_activation_import_run_revoke_cleanup_and_recovery(service, tmp_path):
    key = service.put().json()["activation_key"]
    game = FakeGame()
    with AccountRepository(tmp_path / "accounts.sqlite") as repo:
        accounts = import_accounts("synthetic:synthetic-password", repo, MemorySecretStore())
        account = accounts.accounts[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app)) as http:
            client = make_client(service, http)
            coordinator = Coordinator(repo, game=game, lease=LicenseAdmission(client))
            assert (await coordinator.start(account.id)).state == AccountState.BLOCKED
            await client.activate(key)
            assert (await coordinator.start(account.id)).state == AccountState.RUNNING
            assert game.running == {account.id}
            assert service.put(revision=2, status="revoked").status_code == 200
            with pytest.raises(LicenseUnavailable):
                await client.renew()
            await coordinator.check_leases()
            assert repo.get(account.id).state == AccountState.STOPPED
            assert not game.running
            assert (await coordinator.start(account.id)).state == AccountState.BLOCKED
            await coordinator.close()
            await client.aclose()
        repo.update_state(account.id, AccountState.STARTING)
    with AccountRepository(tmp_path / "accounts.sqlite") as reopened:
        restarted = Coordinator(reopened, game=FakeGame())
        await restarted.recover()
        assert reopened.get(account.id).state == AccountState.INTERRUPTED
        assert (await restarted.start(account.id)).state == AccountState.BLOCKED
        await restarted.close()
        await asyncio.sleep(0)
