"""Per-account serialized lifecycle with bounded launches and durable recovery."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .clock import RealClock
from .contracts import Clock, GameAdapter, LeaseGate
from .models import (
    ACTIVE_STATES,
    Account,
    AccountState,
    Admission,
    Event,
    StateConflict,
    TransientError,
    UnavailableError,
    ValidationError,
)
from .repository import AccountRepository


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    delay: float = 0.1
    launch_timeout: float = 30.0
    cleanup_timeout: float = 10.0

    def __post_init__(self) -> None:
        if (
            type(self.attempts) is not int
            or not 1 <= self.attempts <= 10
            or not 0 <= self.delay <= 3600
            or not 0 < self.launch_timeout <= 3600
            or not 0 < self.cleanup_timeout <= 300
        ):
            raise ValidationError("invalid_retry_policy")


class Coordinator:
    """Own this object on one asyncio loop; repo reads are safe from other threads.

    max_concurrency bounds in-progress launches (1..50). At most 50 account sessions
    are admitted across simultaneous batches. start() completes at RUNNING/failure;
    a successful simulator session remains RUNNING until stopped. Subscriber callbacks
    run on the loop thread and must bridge into Qt with a signal.
    """

    def __init__(
        self,
        repository: AccountRepository,
        *,
        game: GameAdapter | None = None,
        lease: LeaseGate | None = None,
        clock: Clock | None = None,
        max_concurrency: int = 5,
        retry: RetryPolicy | None = None,
        lease_poll_interval: float = 1.0,
    ) -> None:
        from fsm_panel.adapters.unavailable import DenyLease, NativeGameUnavailable

        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 50:
            raise ValidationError("concurrency_must_be_1_to_50")
        if not 0.01 <= lease_poll_interval <= 30:
            raise ValidationError("invalid_lease_poll_interval")
        self.repository = repository
        self.game = game or NativeGameUnavailable()
        self.lease = lease or DenyLease()
        self.clock = clock or RealClock()
        self.retry = retry or RetryPolicy()
        self.max_concurrency = max_concurrency
        self.lease_poll_interval = lease_poll_interval
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._starts: dict[str, asyncio.Task[Account]] = {}
        self._sessions: set[str] = set()
        self._subscribers: list[Callable[[Event], None]] = []
        self._closing = False
        self._recovering = False
        self._monitor: asyncio.Task[None] | None = None
        self.subscriber_errors = 0

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, event: Event) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception:
                # Observers cannot break cleanup or leak arbitrary exception payloads.
                self.subscriber_errors += 1

    def _state(self, account_id: str, state: AccountState, code: str | None = None) -> Account:
        self._emit(self.repository.update_state(account_id, state, code=code))
        return self.repository.get(account_id)

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def _admitted(self, account_id: str) -> bool:
        try:
            admission = await asyncio.wait_for(self.lease.admit(account_id), timeout=10)
            # A truthy string, malformed response, or unexpected exception never grants access.
            return isinstance(admission, Admission) and admission.allowed is True
        except Exception:
            return False

    async def _cleanup(self, account_id: str, *, recovery: bool = False) -> bool:
        try:
            action = self.game.recover if recovery else self.game.stop
            await asyncio.wait_for(
                action(self.repository.get(account_id)), timeout=self.retry.cleanup_timeout
            )
            return not self.repository.owned_processes(account_id)
        except Exception:
            return False

    async def start(self, account_id: str) -> Account:
        self.repository.get(account_id)
        if self._closing or self._recovering:
            raise StateConflict("coordinator_not_accepting_starts")
        async with self._lock(account_id):
            account = self.repository.get(account_id)
            if self._closing or self._recovering:
                raise StateConflict("coordinator_not_accepting_starts")
            if account.state in ACTIVE_STATES:
                if account_id not in self._sessions:
                    raise StateConflict("recovery_required")
                return account
            if account.error_code == "cleanup_required":
                raise StateConflict("cleanup_required")
            if len(self._sessions) >= 50:
                return self._state(account_id, AccountState.BLOCKED, "session_capacity_reached")
            self._sessions.add(account_id)
            task = asyncio.create_task(self._launch(account_id), name=f"launch-{account_id}")
            self._starts[account_id] = task
            try:
                return await task
            finally:
                self._starts.pop(account_id, None)
                if self.repository.get(account_id).state not in {
                    AccountState.RUNNING,
                    AccountState.PAUSED,
                }:
                    self._sessions.discard(account_id)

    async def _launch(self, account_id: str) -> Account:
        try:
            async with self._semaphore:
                for attempt in range(1, self.retry.attempts + 1):
                    if not await self._admitted(account_id):
                        return self._state(account_id, AccountState.BLOCKED, "lease_denied")
                    account = self._state(account_id, AccountState.STARTING)
                    try:
                        await asyncio.wait_for(
                            self.game.launch(account), timeout=self.retry.launch_timeout
                        )
                        if not await self._admitted(account_id):
                            cleaned = await self._cleanup(account_id)
                            return self._state(
                                account_id,
                                AccountState.BLOCKED if cleaned else AccountState.FAILED,
                                "lease_denied" if cleaned else "cleanup_required",
                            )
                        if self._monitor is None or self._monitor.done():
                            self._monitor = asyncio.create_task(
                                self._watch_leases(), name="lease-watch"
                            )
                        return self._state(account_id, AccountState.RUNNING)
                    except TransientError:
                        if not await self._cleanup(account_id):
                            return self._state(account_id, AccountState.FAILED, "cleanup_required")
                        if attempt == self.retry.attempts:
                            return self._state(
                                account_id, AccountState.FAILED, "launch_retries_exhausted"
                            )
                        self._emit(self.repository.record_retry(account_id, attempt))
                        await self.clock.sleep(self.retry.delay)
                    except UnavailableError:
                        if not await self._cleanup(account_id):
                            return self._state(account_id, AccountState.FAILED, "cleanup_required")
                        return self._state(
                            account_id, AccountState.UNAVAILABLE, "native_behavior_unverified"
                        )
                    except Exception:
                        # Timeout or unclassified outcome is never automatically retried.
                        cleaned = await self._cleanup(account_id)
                        return self._state(
                            account_id,
                            AccountState.FAILED,
                            "launch_failed" if cleaned else "cleanup_required",
                        )
            raise AssertionError("unreachable")
        except asyncio.CancelledError:
            # Persist cancellation even when cancelled while queued or awaiting a lease.
            self._state(account_id, AccountState.STOPPING, "cancel_requested")
            cleanup = asyncio.create_task(self._cleanup(account_id))
            while True:
                try:
                    cleaned = await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    # Multiple callers may cancel the same start; finish bounded cleanup.
                    continue
            self._state(
                account_id,
                AccountState.STOPPED if cleaned else AccountState.FAILED,
                "cancelled" if cleaned else "cleanup_required",
            )
            raise

    async def start_batch(self, account_ids: Iterable[str]) -> list[Account]:
        ids = list(dict.fromkeys(account_ids))
        if len(ids) > 50:
            raise ValidationError("batch_limit_50")
        for account_id in ids:
            self.repository.get(account_id)
        tasks = [asyncio.create_task(self.start(account_id)) for account_id in ids]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if task.cancelling() == 0:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for result in results:
            if isinstance(result, Exception):
                raise result
        return [self.repository.get(account_id) for account_id in ids]

    async def _interrupt_start(self, account_id: str) -> None:
        task = self._starts.get(account_id)
        if task is not None and not task.done():
            # Only one cancellation is sent; repeated stop calls cannot interrupt cleanup.
            if task.cancelling() == 0:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop(self, account_id: str) -> Account:
        self.repository.get(account_id)
        await self._interrupt_start(account_id)
        async with self._lock(account_id):
            account = self.repository.get(account_id)
            if account.state == AccountState.STOPPED:
                self._sessions.discard(account_id)
                return account
            self._state(account_id, AccountState.STOPPING)
            cleaned = await self._cleanup(account_id)
            if cleaned:
                self._sessions.discard(account_id)
            return self._state(
                account_id,
                AccountState.STOPPED if cleaned else AccountState.FAILED,
                None if cleaned else "cleanup_required",
            )

    async def cancel(self, account_id: str) -> Account:
        return await self.stop(account_id)

    async def pause(self, account_id: str) -> Account:
        async with self._lock(account_id):
            account = self.repository.get(account_id)
            if account.state != AccountState.RUNNING:
                return account
            try:
                await asyncio.wait_for(self.game.pause(account), timeout=self.retry.launch_timeout)
                return self._state(account_id, AccountState.PAUSED)
            except Exception:
                return self._state(account_id, AccountState.RUNNING, "pause_failed")

    async def resume(self, account_id: str) -> Account:
        async with self._lock(account_id):
            account = self.repository.get(account_id)
            if account.state != AccountState.PAUSED:
                return account
            if not await self._admitted(account_id):
                return self._state(account_id, AccountState.PAUSED, "lease_denied")
            try:
                await asyncio.wait_for(self.game.resume(account), timeout=self.retry.launch_timeout)
                return self._state(account_id, AccountState.RUNNING)
            except Exception:
                return self._state(account_id, AccountState.PAUSED, "resume_failed")

    async def recover(self) -> list[Account]:
        """Clean interrupted sessions; never automatically restart accounts or effects."""
        if self._recovering:
            raise StateConflict("recovery_in_progress")
        self._recovering = True
        try:
            for account_id in tuple(self._starts):
                await self._interrupt_start(account_id)
            self.repository.recover_effects()
            recover_ids = {
                account.id
                for account in self.repository.list_accounts()
                if account.state in ACTIVE_STATES or account.error_code == "cleanup_required"
            }
            recover_ids.update(
                str(item["account_id"]) for item in self.repository.owned_processes()
            )
            for account_id in recover_ids:
                async with self._lock(account_id):
                    cleaned = await self._cleanup(account_id, recovery=True)
                    if cleaned:
                        self._sessions.discard(account_id)
                    self._state(
                        account_id,
                        AccountState.INTERRUPTED if cleaned else AccountState.FAILED,
                        "recovered_after_interruption" if cleaned else "cleanup_required",
                    )
            return self.repository.list_accounts()
        finally:
            self._recovering = False

    async def check_leases(self) -> list[Account]:
        """Fail closed on revocation/offline; call explicitly for deterministic clock tests."""
        stopped: list[Account] = []
        for account_id in tuple(self._sessions):
            if self.repository.get(account_id).state not in {
                AccountState.RUNNING,
                AccountState.PAUSED,
            }:
                continue
            if not await self._admitted(account_id):
                account = await self.stop(account_id)
                if account.state == AccountState.STOPPED:
                    account = self._state(account_id, AccountState.STOPPED, "lease_lost")
                stopped.append(account)
        return stopped

    async def _watch_leases(self) -> None:
        while self._sessions and not self._closing:
            await self.clock.sleep(self.lease_poll_interval)
            if not self._closing:
                await self.check_leases()

    async def close(self) -> None:
        self._closing = True
        if self._monitor is not None and not self._monitor.done():
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
        ids = {
            account.id
            for account in self.repository.list_accounts()
            if account.state in ACTIVE_STATES or account.error_code == "cleanup_required"
        }
        ids.update(self._starts)
        await asyncio.gather(*(self.stop(account_id) for account_id in ids))
