import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fsm_panel.adapters.fakes import FakeClock, FakeGame, FakeLease
from fsm_panel.core import (
    AccountRepository,
    AccountState,
    Coordinator,
    DuplicateEffect,
    EffectState,
    ExternalEffectsLedger,
    RetryPolicy,
    StateConflict,
    TransientError,
)


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancelling_stop_cannot_leave_an_unmonitored_live_session(cancellations):
    class DelayedStop(FakeGame):
        def __init__(self):
            super().__init__(launch_delay=0)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stop(self, account):
            self.entered.set()
            await self.release.wait()
            await super().stop(account)

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_cancel_stop")
        game = DelayedStop()
        coordinator = Coordinator(repository, game=game, lease=FakeLease())
        await coordinator.start(account.id)
        stopping = asyncio.create_task(coordinator.stop(account.id))
        await asyncio.wait_for(game.entered.wait(), timeout=2)
        for _ in range(cancellations):
            stopping.cancel()
            await asyncio.sleep(0)
        game.release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert account.id not in game.running
            assert repository.get(account.id).state == AccountState.STOPPED
        finally:
            await coordinator.close()


def test_adapter_self_cancellation_cannot_spin_forever_during_launch_cleanup():
    # A cancelled cleanup child must be treated as an unknown cleanup result. Run
    # the actual event loop in a child because a cancellation busy-loop would also
    # prevent an asyncio.wait_for timeout from firing in this test process.
    program = r"""
import asyncio
from fsm_panel.adapters.fakes import FakeGame, FakeLease
from fsm_panel.core import AccountRepository, Coordinator

class SelfCancelledStop(FakeGame):
    async def stop(self, account):
        raise asyncio.CancelledError()

async def main():
    with AccountRepository() as repository:
        account = repository.add_account("synthetic_self_cancel")
        game = SelfCancelledStop(launch_delay=60)
        coordinator = Coordinator(repository, game=game, lease=FakeLease())
        launching = asyncio.create_task(coordinator.start(account.id))
        while game.launching != 1:
            await asyncio.sleep(0)
        launching.cancel()
        try:
            await launching
        except asyncio.CancelledError:
            pass
        assert repository.get(account.id).error_code == "cleanup_required"

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=4,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_failed_cancel_cleanup_blocks_new_launch_and_recovery_does_not_restart():
    class UncertainStop(FakeGame):
        def __init__(self):
            super().__init__(launch_delay=60)
            self.launched = asyncio.Event()
            self.can_stop = False

        async def launch(self, account):
            # The adapter acquired a resource before the caller can observe launch.
            self.running.add(account.id)
            self.launched.set()
            await super().launch(account)

        async def stop(self, account):
            if not self.can_stop:
                raise OSError("synthetic cleanup unavailable")
            await super().stop(account)

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_unknown_cleanup")
        game = UncertainStop()
        coordinator = Coordinator(
            repository, game=game, lease=FakeLease(), retry=RetryPolicy(cleanup_timeout=1)
        )
        launching = asyncio.create_task(coordinator.start(account.id))
        await asyncio.wait_for(game.launched.wait(), timeout=2)
        launching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launching
        assert repository.get(account.id).error_code == "cleanup_required"
        attempts = game.launch_attempts[account.id]
        with pytest.raises(StateConflict, match="cleanup_required"):
            await coordinator.start(account.id)
        game.can_stop = True
        await coordinator.recover()
        assert repository.get(account.id).state == AccountState.INTERRUPTED
        assert not game.running
        assert game.launch_attempts[account.id] == attempts
        await coordinator.close()


async def test_persisted_paused_session_cannot_resume_before_recovery():
    with AccountRepository() as repository:
        account = repository.add_account("synthetic_unrecovered_resume")
        repository.update_state(account.id, AccountState.PAUSED)
        game = FakeGame(launch_delay=0)
        # Model a resource that survived the previous coordinator's crash.
        game.running.add(account.id)
        game.paused.add(account.id)
        coordinator = Coordinator(repository, game=game, lease=FakeLease())
        try:
            with pytest.raises(StateConflict, match="recovery_required"):
                await coordinator.resume(account.id)
            assert game.paused == {account.id}
            assert ("resume", account.id) not in game.operations
            await coordinator.recover()
            assert not game.running
            assert repository.get(account.id).state == AccountState.INTERRUPTED
        finally:
            await coordinator.close()


async def test_caller_cancellation_during_retry_cleanup_is_not_an_adapter_self_cancel():
    class RetryCleanup(FakeGame):
        def __init__(self):
            super().__init__(launch_delay=0)
            self.cleaning = asyncio.Event()
            self.release = asyncio.Event()

        async def launch(self, account):
            self.running.add(account.id)
            raise TransientError("synthetic_retry")

        async def stop(self, account):
            self.cleaning.set()
            await self.release.wait()
            await super().stop(account)

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_cancel_retry_cleanup")
        game = RetryCleanup()
        coordinator = Coordinator(repository, game=game, lease=FakeLease())
        launching = asyncio.create_task(coordinator.start(account.id))
        await asyncio.wait_for(game.cleaning.wait(), timeout=2)
        launching.cancel()
        await asyncio.sleep(0)
        game.release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await launching
            assert account.id not in game.running
            assert repository.get(account.id).state == AccountState.STOPPED
        finally:
            await coordinator.close()


async def test_concurrent_lease_sweep_waits_for_an_existing_revocation_cleanup():
    class DelayedStop(FakeGame):
        def __init__(self):
            super().__init__(launch_delay=0)
            self.cleaning = asyncio.Event()
            self.release = asyncio.Event()

        async def stop(self, account):
            self.cleaning.set()
            await self.release.wait()
            await super().stop(account)

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_concurrent_lease_sweeps")
        game, lease = DelayedStop(), FakeLease()
        # Keep the periodic timer parked so both explicit sweeps are deterministic.
        coordinator = Coordinator(repository, game=game, lease=lease, clock=FakeClock())
        await coordinator.start(account.id)
        lease.allowed = False
        first = asyncio.create_task(coordinator.check_leases())
        second = None
        try:
            await asyncio.wait_for(game.cleaning.wait(), timeout=2)
            assert repository.get(account.id).state == AccountState.STOPPING
            entered = asyncio.Event()

            async def another_sweep():
                entered.set()
                return await coordinator.check_leases()

            second = asyncio.create_task(another_sweep())
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not second.done(), "Lease sweep returned while revoked session was still live"
            assert account.id in game.running
            game.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
            assert not game.running
            assert repository.get(account.id).state == AccountState.STOPPED
            assert repository.get(account.id).error_code == "lease_lost"
        finally:
            game.release.set()
            await asyncio.gather(first, *([second] if second is not None else []))
            await coordinator.close()


def test_effect_claim_race_and_unknown_reopen_never_repeat_submission(tmp_path):
    path = tmp_path / "effects.sqlite"
    with AccountRepository(path) as first, AccountRepository(path) as second:
        account = first.add_account("synthetic_effect_race")
        reserved = ExternalEffectsLedger(first).reserve(account.id, "trade", "synthetic-intent")
        barrier = threading.Barrier(2)

        def claim(repository):
            barrier.wait(timeout=2)
            try:
                ExternalEffectsLedger(repository).begin(reserved.id)
                return "may_submit_once"
            except StateConflict:
                return "must_not_submit"

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(claim, [first, second]))
        assert sorted(results) == ["may_submit_once", "must_not_submit"]

    with AccountRepository(path) as restarted:
        ledger = ExternalEffectsLedger(restarted)
        assert ledger.recover() == 1
        assert restarted.get_effect(reserved.id).state == EffectState.UNKNOWN
        with pytest.raises(StateConflict):
            ledger.begin(reserved.id)
        with pytest.raises(DuplicateEffect):
            ledger.reserve(account.id, "trade", "synthetic-intent")
        assert ledger.recover() == 0
