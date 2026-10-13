import asyncio

import pytest

from fsm_panel.adapters.fakes import FakeClock, FakeGame, FakeLease
from fsm_panel.core import (
    AccountRepository,
    AccountState,
    Admission,
    Coordinator,
    RetryPolicy,
    StateConflict,
    ValidationError,
)


@pytest.fixture
def repo():
    with AccountRepository() as repository:
        yield repository


async def until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("condition did not become true")


@pytest.mark.parametrize("kind", ["raises", "malformed", "truthy"])
async def test_lease_errors_and_non_boolean_values_fail_closed(repo, kind):
    class FaultyLease:
        async def admit(self, account_id):
            if kind == "raises":
                raise RuntimeError("synthetic secret not suitable for logs")
            if kind == "truthy":
                return Admission("true")
            return True

    account = repo.add_account("synthetic")
    game = FakeGame(launch_delay=0)
    coordinator = Coordinator(repo, game=game, lease=FaultyLease())
    assert (await coordinator.start(account.id)).state == AccountState.BLOCKED
    assert game.launch_attempts[account.id] == 0
    assert "synthetic secret" not in repr(repo.events())
    await coordinator.close()


async def test_lease_lost_during_launch_cleans_before_admitting_running(repo):
    account = repo.add_account("synthetic")
    clock, lease = FakeClock(), FakeLease()
    game = FakeGame(clock=clock, launch_delay=3)
    coordinator = Coordinator(repo, game=game, lease=lease)
    launch = asyncio.create_task(coordinator.start(account.id))
    await until(lambda: clock.pending_sleeps == 1)
    lease.allowed = False
    clock.advance(3)
    assert (await launch).state == AccountState.BLOCKED
    assert not game.running
    await coordinator.close()


async def test_automatic_lease_watch_stops_without_new_user_command(repo):
    account = repo.add_account("synthetic")
    clock, lease = FakeClock(), FakeLease()
    game = FakeGame(launch_delay=0)
    coordinator = Coordinator(repo, game=game, lease=lease, clock=clock)
    await coordinator.start(account.id)
    await until(lambda: clock.pending_sleeps == 1)
    lease.allowed = False
    clock.advance(1)
    await until(lambda: repo.get(account.id).error_code == "lease_lost")
    assert not game.running
    await coordinator.close()


async def test_exhausted_transient_retries_are_bounded(repo):
    account = repo.add_account("synthetic")
    game = FakeGame(launch_delay=0)
    for _ in range(5):
        game.fail_next(account.id)
    coordinator = Coordinator(repo, game=game, lease=FakeLease(), retry=RetryPolicy(delay=0))
    assert (await coordinator.start(account.id)).error_code == "launch_retries_exhausted"
    assert game.launch_attempts[account.id] == 3
    assert not game.running
    await coordinator.close()


async def test_whole_batch_cancellation_cleans_running_and_queued_launches(repo):
    accounts = [repo.add_account(f"synthetic_{index}") for index in range(10)]
    clock = FakeClock()
    game = FakeGame(clock=clock, launch_delay=100)
    coordinator = Coordinator(repo, game=game, lease=FakeLease(), max_concurrency=2)
    batch = asyncio.create_task(coordinator.start_batch(account.id for account in accounts))
    await until(lambda: game.launching == 2)
    batch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch
    assert not game.running and game.launching == 0
    assert all(account.state == AccountState.STOPPED for account in repo.list_accounts())
    await coordinator.close()


async def test_repeated_cancellation_does_not_interrupt_cleanup(repo):
    class SlowCleanup(FakeGame):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stop(self, account):
            self.entered.set()
            await self.release.wait()
            await super().stop(account)

    account = repo.add_account("synthetic")
    game = SlowCleanup(launch_delay=100)
    coordinator = Coordinator(repo, game=game, lease=FakeLease())
    launch = asyncio.create_task(coordinator.start(account.id))
    await until(lambda: game.launching == 1)
    launch.cancel()
    await game.entered.wait()
    launch.cancel()
    await asyncio.sleep(0)
    game.release.set()
    with pytest.raises(asyncio.CancelledError):
        await launch
    assert repo.get(account.id).state == AccountState.STOPPED
    await coordinator.close()


async def test_cleanup_timeout_requires_recovery(repo):
    class HungCleanup(FakeGame):
        async def stop(self, account):
            await asyncio.sleep(100)

    account = repo.add_account("synthetic")
    game = HungCleanup(launch_delay=0)
    coordinator = Coordinator(
        repo, game=game, lease=FakeLease(), retry=RetryPolicy(cleanup_timeout=0.01)
    )
    await coordinator.start(account.id)
    assert (await coordinator.stop(account.id)).error_code == "cleanup_required"
    assert repo.get(account.id).state == AccountState.FAILED
    await coordinator.recover()
    assert repo.get(account.id).error_code == "cleanup_required"
    await coordinator.close()


async def test_existing_owned_process_record_cannot_be_silently_claimed_clean(repo):
    account = repo.add_account("synthetic")
    repo.remember_process("synthetic_token", account.id, 123, 100.0, "synthetic.exe")
    coordinator = Coordinator(repo)
    await coordinator.recover()
    assert repo.get(account.id).error_code == "cleanup_required"
    assert len(repo.owned_processes()) == 1
    await coordinator.close()


async def test_resume_and_pause_adapter_failures_preserve_last_known_state(repo):
    class FaultyControls(FakeGame):
        fail_pause = True

        async def pause(self, account):
            if self.fail_pause:
                raise RuntimeError("synthetic")
            await super().pause(account)

        async def resume(self, account):
            raise RuntimeError("synthetic")

    account = repo.add_account("synthetic")
    game = FaultyControls(launch_delay=0)
    coordinator = Coordinator(repo, game=game, lease=FakeLease())
    await coordinator.start(account.id)
    assert (await coordinator.pause(account.id)).error_code == "pause_failed"
    game.fail_pause = False
    await coordinator.pause(account.id)
    assert (await coordinator.resume(account.id)).error_code == "resume_failed"
    assert repo.get(account.id).state == AccountState.PAUSED
    await coordinator.close()


async def test_closed_coordinator_rejects_new_work_and_large_batches(repo):
    accounts = [repo.add_account(f"synthetic_{index}") for index in range(51)]
    coordinator = Coordinator(repo)
    with pytest.raises(ValidationError, match="batch_limit_50"):
        await coordinator.start_batch(account.id for account in accounts)
    await coordinator.close()
    with pytest.raises(StateConflict):
        await coordinator.start(accounts[0].id)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempts": True},
        {"attempts": 11},
        {"delay": -1},
        {"launch_timeout": 0},
        {"cleanup_timeout": -1},
    ],
)
def test_invalid_retry_policy_rejected(kwargs):
    with pytest.raises(ValidationError):
        RetryPolicy(**kwargs)


def test_invalid_lease_interval_and_repeated_unsubscribe_are_safe(repo):
    with pytest.raises(ValidationError):
        Coordinator(repo, lease_poll_interval=0)
    coordinator = Coordinator(repo)
    unsubscribe = coordinator.subscribe(lambda event: None)
    unsubscribe()
    unsubscribe()
