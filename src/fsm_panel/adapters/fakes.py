"""Explicit simulator adapters. These do not represent recovered Steam/CS2 behavior."""

import asyncio
import heapq
import itertools
from collections import defaultdict

from fsm_panel.core.clock import RealClock as RealClock
from fsm_panel.core.contracts import Clock
from fsm_panel.core.models import Account, Admission, StateConflict, TransientError


class FakeClock:
    """Manually advanced monotonic clock. advance() wakes due sleepers on the next loop turn."""

    def __init__(self, now: float = 0) -> None:
        self._now = float(now)
        self._counter = itertools.count()
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("negative_sleep")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now + seconds, next(self._counter), future))
        await future

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock_cannot_go_backwards")
        self._now += seconds
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)

    @property
    def pending_sleeps(self) -> int:
        return sum(not item[2].done() for item in self._sleepers)


class FakeLease:
    """Simulator-only lease admission; revocation/expiry can be changed during a test."""

    def __init__(self, allowed: bool = True, *, clock: Clock | None = None,
                 valid_until: float | None = None) -> None:
        self.allowed = allowed
        self.clock = clock or RealClock()
        self.valid_until = valid_until
        self.calls: list[str] = []

    async def admit(self, account_id: str) -> Admission:
        self.calls.append(account_id)
        valid = self.allowed and (
            self.valid_until is None or self.clock.monotonic() < self.valid_until
        )
        return Admission(valid, "simulator_lease" if valid else "simulator_lease_denied")


class FakeGame:
    """Usable local lifecycle simulator with deterministic failure injection."""

    def __init__(self, *, clock: Clock | None = None, launch_delay: float = 0.02) -> None:
        self.clock = clock or RealClock()
        self.launch_delay = launch_delay
        self.running: set[str] = set()
        self.paused: set[str] = set()
        self.operations: list[tuple[str, str]] = []
        self.failures: dict[str, list[Exception]] = defaultdict(list)
        self.launch_attempts: dict[str, int] = defaultdict(int)
        self.launching = 0
        self.max_launching = 0

    def fail_next(self, account_id: str, error: Exception | None = None) -> None:
        self.failures[account_id].append(error or TransientError("simulated_launch_failure"))

    async def launch(self, account: Account) -> None:
        self.operations.append(("launch", account.id))
        self.launch_attempts[account.id] += 1
        self.launching += 1
        self.max_launching = max(self.launching, self.max_launching)
        try:
            await self.clock.sleep(self.launch_delay)
            if self.failures[account.id]:
                raise self.failures[account.id].pop(0)
            self.running.add(account.id)
        finally:
            self.launching -= 1

    async def pause(self, account: Account) -> None:
        if account.id not in self.running:
            raise StateConflict("simulator_not_running")
        self.operations.append(("pause", account.id))
        self.paused.add(account.id)

    async def resume(self, account: Account) -> None:
        if account.id not in self.running:
            raise StateConflict("simulator_not_running")
        self.operations.append(("resume", account.id))
        self.paused.discard(account.id)

    async def stop(self, account: Account) -> None:
        self.operations.append(("stop", account.id))
        self.running.discard(account.id)
        self.paused.discard(account.id)

    async def recover(self, account: Account) -> None:
        self.operations.append(("recover", account.id))
        await self.stop(account)


class MemorySecretStore:
    """Plaintext in-memory fake for tests/simulator only; never a production fallback."""

    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    def put(self, reference: str, value: bytes) -> None:
        self._values[reference] = bytes(value)

    def get(self, reference: str) -> bytes:
        return self._values[reference]

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)

    def __repr__(self) -> str:
        return f"MemorySecretStore(entries={len(self._values)}, simulator_only=True)"
