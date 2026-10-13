import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fsm_panel.adapters.fakes import FakeClock, FakeGame, FakeLease
from fsm_panel.adapters.unavailable import NativeGameUnavailable
from fsm_panel.core import (
    AccountRepository,
    AccountState,
    Coordinator,
    RetryPolicy,
    StateConflict,
    ValidationError,
)


async def spin_until(predicate, turns=1000):
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    pytest.fail("condition did not become true")


@pytest.fixture
def repo():
    with AccountRepository() as repository:
        yield repository


async def test_lifecycle_events_are_ordered_and_stop_ignores_license(repo):
    account = repo.add_account("synthetic")
    game, lease = FakeGame(launch_delay=0), FakeLease()
    coordinator = Coordinator(repo, game=game, lease=lease)
    events = []
    unsubscribe = coordinator.subscribe(events.append)
    assert (await coordinator.start(account.id)).state == AccountState.RUNNING
    assert (await coordinator.pause(account.id)).state == AccountState.PAUSED
    lease.allowed = False
    assert (await coordinator.resume(account.id)).state == AccountState.PAUSED
    calls_before_stop = len(lease.calls)
    assert (await coordinator.stop(account.id)).state == AccountState.STOPPED
    assert len(lease.calls) == calls_before_stop
    assert not game.running
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    unsubscribe()
    await coordinator.close()


async def test_default_license_and_unknown_native_behavior_fail_closed(repo):
    account = repo.add_account("synthetic")
    coordinator = Coordinator(repo)
    assert (await coordinator.start(account.id)).state == AccountState.BLOCKED
    await coordinator.close()
    coordinator = Coordinator(repo, game=NativeGameUnavailable(), lease=FakeLease())
    assert (await coordinator.start(account.id)).state == AccountState.UNAVAILABLE
    await coordinator.close()


async def test_50_account_batch_is_bounded_and_duplicate_starts_serialize(repo):
    accounts = [repo.add_account(f"synthetic_{index}") for index in range(50)]
    game = FakeGame(launch_delay=0.001)
    coordinator = Coordinator(repo, game=game, lease=FakeLease(), max_concurrency=7)
    results = await coordinator.start_batch(account.id for account in accounts)
    assert all(account.state == AccountState.RUNNING for account in results)
    assert game.max_launching <= 7
    assert len(game.running) == 50
    await asyncio.gather(*(coordinator.start(accounts[0].id) for _ in range(20)))
    assert game.launch_attempts[accounts[0].id] == 1
    extra = repo.add_account("synthetic_extra")
    assert (await coordinator.start(extra.id)).error_code == "session_capacity_reached"
    await coordinator.close()
    assert not game.running


async def test_cancel_during_launch_cleans_and_does_not_retry(repo):
    account = repo.add_account("synthetic")
    clock, game = FakeClock(), FakeGame(launch_delay=100)
    game.clock = clock
    coordinator = Coordinator(repo, game=game, lease=FakeLease(), clock=clock)
    launch = asyncio.create_task(coordinator.start(account.id))
    await spin_until(lambda: game.launching == 1)
    result = await coordinator.cancel(account.id)
    assert result.state == AccountState.STOPPED
    assert result.error_code == "cancelled"
    assert isinstance((await asyncio.gather(launch, return_exceptions=True))[0], asyncio.CancelledError)
    assert not game.running and game.launching == 0
    assert game.launch_attempts[account.id] == 1
    await coordinator.close()


async def test_retry_only_confirmed_transient_failure_and_cancel_backoff(repo):
    account = repo.add_account("synthetic")
    clock, game = FakeClock(), FakeGame(launch_delay=0)
    game.fail_next(account.id)
    coordinator = Coordinator(repo, game=game, lease=FakeLease(), clock=clock,
                              retry=RetryPolicy(delay=10))
    launch = asyncio.create_task(coordinator.start(account.id))
    await spin_until(lambda: clock.pending_sleeps == 1)
    assert game.launch_attempts[account.id] == 1
    clock.advance(10)
    assert (await launch).state == AccountState.RUNNING
    assert game.launch_attempts[account.id] == 2
    assert any(event.kind == "launch_retry" for event in repo.events())
    await coordinator.stop(account.id)
    game.fail_next(account.id)
    launch = asyncio.create_task(coordinator.start(account.id))
    await spin_until(lambda: any(event.kind == "launch_retry" and event.sequence > 7
                               for event in repo.events()))
    await coordinator.cancel(account.id)
    await asyncio.gather(launch, return_exceptions=True)
    assert not game.running
    await coordinator.close()


async def test_raw_adapter_errors_are_never_journaled_or_retried(repo):
    account = repo.add_account("synthetic")
    game = FakeGame(launch_delay=0)
    game.fail_next(account.id, RuntimeError("synthetic_password_do_not_log"))
    coordinator = Coordinator(repo, game=game, lease=FakeLease())
    result = await coordinator.start(account.id)
    assert result.state == AccountState.FAILED
    assert game.launch_attempts[account.id] == 1
    assert "synthetic_password" not in repr(repo.events())
    await coordinator.close()


async def test_cleanup_failure_blocks_retry_until_explicit_stop_succeeds(repo):
    class DirtyGame(FakeGame):
        fail_cleanup = True

        async def stop(self, account):
            if self.fail_cleanup:
                raise OSError("synthetic cleanup failure")
            await super().stop(account)

    account = repo.add_account("synthetic")
    game = DirtyGame(launch_delay=0)
    game.fail_next(account.id)
    coordinator = Coordinator(repo, game=game, lease=FakeLease())
    assert (await coordinator.start(account.id)).error_code == "cleanup_required"
    with pytest.raises(StateConflict, match="cleanup_required"):
        await coordinator.start(account.id)
    assert game.launch_attempts[account.id] == 1
    game.fail_cleanup = False
    await coordinator.stop(account.id)
    assert (await coordinator.start(account.id)).state == AccountState.RUNNING
    await coordinator.close()


async def test_restart_recovery_stops_without_license_and_marks_effects_unknown(tmp_path):
    path = tmp_path / "state.sqlite"
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic")
        repo.update_state(account.id, AccountState.RUNNING)
        effect = repo.reserve_effect(account.id, "trade", "synthetic_key")
    with AccountRepository(path) as repo:
        game = FakeGame()
        coordinator = Coordinator(repo, game=game)
        with pytest.raises(StateConflict, match="recovery_required"):
            await coordinator.start(account.id)
        await coordinator.recover()
        assert repo.get(account.id).state == AccountState.INTERRUPTED
        assert repo.get_effect(effect.id).state.value == "unknown"
        assert ("recover", account.id) in game.operations
        await coordinator.close()


async def test_expired_or_broken_lease_stops_running_sessions(repo):
    account = repo.add_account("synthetic")
    clock = FakeClock()
    lease = FakeLease(clock=clock, valid_until=5)
    game = FakeGame(launch_delay=0)
    coordinator = Coordinator(repo, game=game, lease=lease, clock=clock)
    await coordinator.start(account.id)
    clock.advance(5)
    await coordinator.check_leases()
    assert repo.get(account.id).state == AccountState.STOPPED
    assert repo.get(account.id).error_code == "lease_lost"
    assert not game.running
    await coordinator.close()


async def test_launch_timeout_has_no_retry_and_observer_cannot_break_cleanup(repo):
    account = repo.add_account("synthetic")
    game = FakeGame(launch_delay=1)
    coordinator = Coordinator(repo, game=game, lease=FakeLease(),
                              retry=RetryPolicy(launch_timeout=0.01))
    coordinator.subscribe(lambda event: (_ for _ in ()).throw(ValueError("observer")))
    assert (await coordinator.start(account.id)).state == AccountState.FAILED
    assert game.launch_attempts[account.id] == 1
    assert not game.running
    assert coordinator.subscriber_errors >= 2
    await coordinator.close()


@pytest.mark.parametrize("concurrency", [0, 51, True, -1])
def test_invalid_concurrency_is_rejected(repo, concurrency):
    with pytest.raises(ValidationError):
        Coordinator(repo, max_concurrency=concurrency)


@given(st.lists(st.sampled_from(["start", "pause", "resume", "stop", "revoke", "grant"]),
                min_size=1, max_size=35))
@settings(max_examples=35, deadline=None)
def test_generated_command_sequences_preserve_session_invariants(commands):
    async def scenario():
        with AccountRepository() as repo:
            account = repo.add_account("synthetic_property")
            game, lease = FakeGame(launch_delay=0), FakeLease()
            coordinator = Coordinator(repo, game=game, lease=lease)
            for command in commands:
                if command in {"revoke", "grant"}:
                    lease.allowed = command == "grant"
                    await coordinator.check_leases()
                else:
                    await getattr(coordinator, command)(account.id)
                snapshot = repo.get(account.id)
                assert (account.id in game.running) == (
                    snapshot.state in {AccountState.RUNNING, AccountState.PAUSED}
                )
                assert (account.id in game.paused) == (snapshot.state == AccountState.PAUSED)
                assert repo.events(account_id=account.id)[-1].state == snapshot.state
            await coordinator.close()
            assert not game.running
    asyncio.run(scenario())
