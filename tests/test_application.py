import asyncio
from types import SimpleNamespace

import pytest
from fsm_panel.adapters.fakes import FakeGame
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.licensing import LicenseUnavailable

from fsm_panel.application import LicenseAdmission, LicenseRuntime


class Gate:
    valid = True

    def require_valid(self):
        if not self.valid:
            raise LicenseUnavailable("expired")
        return SimpleNamespace(device_limit=2)


class Client:
    def __init__(self):
        self.gate = Gate()
        self.closed = False

    async def run_renewal(self, stop):
        await stop.wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_license_expiry_stops_existing_work_and_denies_new_admission():
    repository = AccountRepository()
    first = repository.add_account("synthetic_one")
    second = repository.add_account("synthetic_two")
    client = Client()
    coordinator = Coordinator(repository, game=FakeGame(), lease=LicenseAdmission(client))
    notices = []
    runtime = LicenseRuntime(client, coordinator, notices.append)
    task = asyncio.create_task(runtime.run())
    await coordinator.start(first.id)
    assert repository.get(first.id).state.value == "running"
    client.gate.valid = False
    for _ in range(100):
        if repository.get(first.id).state.value == "stopped":
            break
        await asyncio.sleep(0.01)
    assert repository.get(first.id).state.value == "stopped"
    await coordinator.start(second.id)
    assert repository.get(second.id).state.value == "blocked"
    await coordinator.stop(second.id)
    await runtime.close()
    await task
    await coordinator.close()
    assert client.closed
    assert any("запрещены" in notice for notice in notices)
    repository.close()
