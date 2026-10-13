import asyncio
from types import SimpleNamespace

import pytest

from fsm_panel.adapters.fakes import FakeClock, FakeGame, FakeLease
from fsm_panel.core import AccountRepository, AccountState, Coordinator, StateConflict
from fsm_panel.cpu import CpuLimitRuntime


class Service:
    def __init__(self):
        self.calls = []
        self.on_limit = None
        self.fail_stop = False

    async def limit(self, target, percent, *, executable=None):
        self.calls.append(("limit", target.token, percent, str(executable.path)))
        if self.on_limit:
            await self.on_limit()
        return SimpleNamespace(state="delivered")

    async def stop(self, account_id):
        self.calls.append(("stop", account_id))
        if self.fail_stop:
            raise RuntimeError("cleanup_failed")

    async def recover(self):
        self.calls.append(("recover",))

    def sessions(self, account_id=None):
        return []


@pytest.fixture
async def context():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        game, lease = FakeGame(launch_delay=0), FakeLease()
        coordinator = Coordinator(repo, game=game, lease=lease, clock=FakeClock())
        service = Service()
        runtime = CpuLimitRuntime(coordinator, service)
        runtime.bind_game()
        await runtime.recover()
        await coordinator.start(account.id)
        repo.remember_process("owned", account.id, 321, 10.0, "C:/synthetic.exe")
        yield repo, account, coordinator, service, runtime, game, lease
        service.fail_stop = False
        repo.forget_process("owned")
        await runtime.close()
        await coordinator.close()


async def test_owned_target_and_valid_lease_are_both_required(context):
    repo, account, _, service, runtime, _, lease = context
    assert (await runtime.limit("owned", 30, "C:/BES.exe")).state == "delivered"
    assert service.calls[-1] == ("limit", "owned", 30, "C:/BES.exe")
    with pytest.raises(StateConflict, match="owned_process_required"):
        await runtime.limit("foreign", 30, "C:/BES.exe")
    lease.allowed = False
    with pytest.raises(StateConflict, match="cpu_admission_denied"):
        await runtime.limit("owned", 30, "C:/BES.exe")
    lease.allowed = True
    repo.update_state(account.id, AccountState.STOPPED)
    with pytest.raises(StateConflict, match="cpu_admission_denied"):
        await runtime.limit("owned", 30, "C:/BES.exe")
    assert sum(call[0] == "limit" for call in service.calls) == 1


async def test_lease_loss_during_delivery_cleans_before_return(context):
    _, account, _, service, runtime, _, lease = context

    async def revoke():
        lease.allowed = False

    service.on_limit = revoke
    with pytest.raises(StateConflict, match="cpu_admission_lost"):
        await runtime.limit("owned", 50, "C:/BES.exe")
    assert service.calls[-1] == ("stop", account.id)


async def test_cancellation_during_post_delivery_admission_joins_cleanup(context):
    _, account, coordinator, service, runtime, _, _ = context
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.lease

    class Lease:
        calls = 0

        async def admit(self, account_id):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                await release.wait()
            return await original.admit(account_id)

    coordinator.lease = Lease()
    task = asyncio.create_task(runtime.limit("owned", 50, "C:/BES.exe"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.calls[-1] == ("stop", account.id)


async def test_stop_queued_during_limit_runs_after_delivery_and_blocks_late_limit(context):
    repo, account, coordinator, service, runtime, game, _ = context
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay():
        entered.set()
        await release.wait()

    service.on_limit = delay
    limit = asyncio.create_task(runtime.limit("owned", 50, "C:/BES.exe"))
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(coordinator.stop(account.id))
    for _ in range(50):
        if repo.get(account.id).state == AccountState.STOPPING:
            break
        await asyncio.sleep(0)
    assert repo.get(account.id).state == AccountState.STOPPING
    release.set()
    with pytest.raises(StateConflict, match="cpu_admission_lost"):
        await limit
    await stop
    assert account.id not in game.running
    assert service.calls[-1] == ("stop", account.id)
    with pytest.raises(StateConflict, match="cpu_admission_denied"):
        await runtime.limit("owned", 50, "C:/BES.exe")


async def test_lease_expiry_and_cleanup_error_still_stop_game(context):
    repo, account, coordinator, service, _, game, lease = context
    service.fail_stop = True
    lease.allowed = False
    await coordinator.check_leases()
    assert account.id not in game.running
    assert service.calls[-1] == ("stop", account.id)
    assert repo.get(account.id).error_code == "cleanup_required"


@pytest.mark.parametrize("guard", ["disabled", "closing", "unrecovered"])
async def test_disabled_unrecovered_or_closing_runtime_rejects_new_commands(context, guard):
    _, account, _, service, runtime, _, lease = context
    if guard == "disabled":
        runtime.enabled = False
    elif guard == "closing":
        runtime.begin_close()
    else:
        runtime._recovered = False
    with pytest.raises(StateConflict, match="cpu_admission_denied"):
        await runtime.limit("owned", 50, "C:/BES.exe")
    lease.allowed = False
    await runtime.stop(account.id)
    await runtime.recover()
    assert not any(call[0] == "limit" for call in service.calls)


async def test_malformed_lease_never_grants_limit(context):
    _, _, coordinator, service, runtime, _, _ = context

    class MalformedLease:
        async def admit(self, account_id):
            return SimpleNamespace(allowed="yes")

    coordinator.lease = MalformedLease()
    with pytest.raises(StateConflict, match="cpu_admission_denied"):
        await runtime.limit("owned", 50, "C:/BES.exe")
    assert not any(call[0] == "limit" for call in service.calls)
