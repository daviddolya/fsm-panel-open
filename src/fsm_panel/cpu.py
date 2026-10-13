"""Admission and lifecycle composition for an owned BES instance."""

import asyncio
from typing import Any, Protocol

from fsm_panel.adapters.bes import ReviewedBesExecutable
from fsm_panel.adapters.processes import OwnedProcess
from fsm_panel.core import Coordinator
from fsm_panel.core.contracts import GameAdapter
from fsm_panel.core.models import Account, AccountState, Admission, StateConflict


class CpuLimitService(Protocol):
    async def limit(
        self,
        target: OwnedProcess,
        reduction_percent: int,
        *,
        executable: ReviewedBesExecutable | None = None,
    ) -> Any: ...
    async def stop(self, account_id: str) -> None: ...
    async def recover(self) -> None: ...
    def sessions(self, account_id: str | None = None) -> list[Any]: ...


class CpuLimitRuntime:
    """One queue prevents a new limit overtaking stop, recovery or shutdown.

    The service rechecks OS process identities. This layer additionally checks
    account state and admission before and after a potentially slow command.
    """

    def __init__(self, coordinator: Coordinator, service: CpuLimitService, *, enabled: bool = True):
        self.coordinator = coordinator
        self.service = service
        self._lock = asyncio.Lock()
        self._closing = False
        self._recovered = False
        self.enabled = enabled

    def bind_game(self) -> None:
        self.coordinator.game = CpuManagedGame(self.coordinator.game, self)

    async def _allowed(self, account_id: str) -> bool:
        def still_allowed() -> bool:
            return (
                self.enabled
                and not self._closing
                and self._recovered
                and self.coordinator.repository.get(account_id).state
                in {AccountState.RUNNING, AccountState.PAUSED}
            )

        if not still_allowed():
            return False
        try:
            admission = await asyncio.wait_for(self.coordinator.lease.admit(account_id), 10)
            return (
                isinstance(admission, Admission) and admission.allowed is True and still_allowed()
            )
        except Exception:
            return False

    async def limit(self, process_token: str, percent: int, executable: str):
        async with self._lock:
            records = [
                record
                for record in self.coordinator.repository.owned_processes()
                if record["token"] == process_token
            ]
            if len(records) != 1:
                raise StateConflict("owned_process_required")
            record = records[0]
            target = OwnedProcess(
                str(record["token"]),
                str(record["account_id"]),
                int(str(record["pid"])),
                float(str(record["create_time"])),
                str(record["executable"]),
            )
            if not await self._allowed(target.account_id):
                raise StateConflict("cpu_admission_denied")
            try:
                result = await self.service.limit(
                    target,
                    percent,
                    executable=ReviewedBesExecutable(executable),
                )
                if not await self._allowed(target.account_id):
                    raise StateConflict("cpu_admission_lost")
                return result
            except BaseException:
                # Join cleanup even if cancellation arrives after command delivery.
                # The backend bounds each OS wait and journals uncertain outcomes.
                cleanup = asyncio.create_task(self.service.stop(target.account_id))
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            raise
                raise

    async def stop(self, account_id: str) -> None:
        async with self._lock:
            await self.service.stop(account_id)

    async def recover(self) -> None:
        async with self._lock:
            self._recovered = False
            await self.service.recover()
            self._recovered = True

    async def close(self) -> None:
        self.begin_close()
        async with self._lock:
            await self.service.recover()

    def begin_close(self) -> None:
        self._closing = True


class CpuManagedGame:
    def __init__(self, inner: GameAdapter, runtime: CpuLimitRuntime):
        self.inner, self.runtime = inner, runtime

    async def launch(self, account: Account) -> None:
        await self.inner.launch(account)

    async def pause(self, account: Account) -> None:
        await self.inner.pause(account)

    async def resume(self, account: Account) -> None:
        await self.inner.resume(account)

    async def stop(self, account: Account) -> None:
        try:
            await self.runtime.stop(account.id)
        finally:
            # Owned target cleanup remains possible even when BES requires recovery.
            await self.inner.stop(account)

    async def recover(self, account: Account) -> None:
        try:
            await self.runtime.stop(account.id)
        finally:
            await self.inner.recover(account)
