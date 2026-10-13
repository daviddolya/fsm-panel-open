"""License admission and durable core-effect integration for the Steam worker."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fsm_panel.core import AccountRepository, EffectState, ExternalEffectsLedger

from .ipc import WorkerClient
from .models import (
    InventoryResult,
    InventorySpec,
    SteamCredentials,
    TradeError,
    TradeResult,
    TradeState,
)

# Returns a wall-clock deadline in milliseconds, derived from verified online admission.
# Do not return a constant True. Rechecked during active new work; recovery bypasses it.
AdmissionProvider = Callable[[str], Awaitable[int]]


class TradeService:
    def __init__(self, repository: AccountRepository, worker: WorkerClient,
                 admission: AdmissionProvider | None = None) -> None:
        self.repository, self.worker, self.admission = repository, worker, admission
        self.ledger = ExternalEffectsLedger(repository)
        self._lock = asyncio.Lock()

    async def _admit(self, account_id: str) -> int:
        self.repository.get(account_id)
        if self.admission is None:
            raise TradeError("admission_required")
        try:
            deadline = await self.admission(account_id)
        except Exception:
            raise TradeError("admission_required") from None
        now = time.time() * 1000
        if type(deadline) is not int or not now < deadline <= now + 60_000:
            raise TradeError("admission_expired")
        return deadline

    async def _new_work(self, method: str, params: dict[str, Any], timeout_ms: int,
                        deadline: int) -> dict[str, Any]:
        task = asyncio.create_task(self.worker.request(
            method, params, timeout_ms=timeout_ms, admitted_until_ms=deadline))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if not done:
                    await self._admit(params["account_id"])
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, TradeError):
                    pass

    async def inventory(self, account_id: str, credentials: SteamCredentials,
                        inventories: Sequence[InventorySpec] = (InventorySpec(),), *,
                        timeout_ms: int = 30_000) -> InventoryResult:
        async with self._lock:
            deadline = await self._admit(account_id)
            result = await self._new_work("inventory", {
                "account_id": account_id, "credentials": credentials.payload(),
                "inventories": [spec.payload() for spec in inventories],
            }, timeout_ms, deadline)
            return InventoryResult.from_payload(result)

    async def send(self, account_id: str, operation_id: str, trade_url: str,
                   credentials: SteamCredentials,
                   inventories: Sequence[InventorySpec] = (InventorySpec(),), *,
                   allow_partial: bool = True, timeout_ms: int = 30_000) -> TradeResult:
        async with self._lock:
            deadline = await self._admit(account_id)
            # DuplicateEffect is deliberate: calling send again is never a retry path.
            effect = self.ledger.reserve(account_id, "steam_trade", "steam:" + operation_id)
            self.ledger.begin(effect.id)
            try:
                response = await self._new_work("send", {
                    "account_id": account_id, "operation_id": operation_id,
                    "trade_url": trade_url, "credentials": credentials.payload(),
                    "inventories": [spec.payload() for spec in inventories],
                    "allow_partial": allow_partial,
                }, timeout_ms, deadline)
                result = self._result(response, account_id, operation_id)
            except BaseException:
                self.ledger.mark_unknown(effect.id)
                raise
            if result.state == TradeState.UNKNOWN_OUTCOME:
                self.ledger.mark_unknown(effect.id)
            elif result.state in {TradeState.NOT_SENT, TradeState.FAILED}:
                self.ledger.fail_confirmed(effect.id)
            else:
                self.ledger.succeed(effect.id)
            return result

    @staticmethod
    def _result(payload: dict[str, Any], account_id: str, operation_id: str) -> TradeResult:
        result = TradeResult.from_payload(payload)
        if result.account_id != account_id or result.operation_id != operation_id:
            raise TradeError("invalid_worker_response")
        return result

    async def status(self, account_id: str, operation_id: str) -> TradeResult:
        async with self._lock:
            self.repository.get(account_id)
            return self._result(await self.worker.request("status", {
                "account_id": account_id, "operation_id": operation_id,
            }), account_id, operation_id)

    async def confirm(self, account_id: str, operation_id: str, credentials: SteamCredentials,
                      *, timeout_ms: int = 30_000) -> TradeResult:
        return await self._recovery("confirm", account_id, operation_id, credentials, timeout_ms)

    async def reconcile(self, account_id: str, operation_id: str, credentials: SteamCredentials,
                        *, timeout_ms: int = 30_000) -> TradeResult:
        return await self._recovery("reconcile", account_id, operation_id, credentials, timeout_ms)

    async def _recovery(self, method: str, account_id: str, operation_id: str,
                        credentials: SteamCredentials, timeout_ms: int) -> TradeResult:
        async with self._lock:
            self.repository.get(account_id)
            result = self._result(await self.worker.request(method, {
                "account_id": account_id, "operation_id": operation_id,
                "credentials": credentials.payload(),
            }, timeout_ms=timeout_ms), account_id, operation_id)
            if method == "reconcile" and result.state != TradeState.UNKNOWN_OUTCOME:
                for effect in self.repository.list_effects():
                    if (effect.idempotency_key == "steam:" + operation_id
                            and effect.account_id == account_id and effect.state == EffectState.UNKNOWN
                            and result.state != TradeState.NOT_SENT):
                        self.ledger.reconcile(effect.id, succeeded=result.state != TradeState.FAILED)
            return result
