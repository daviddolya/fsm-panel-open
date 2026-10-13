"""License admission and durable core-effect integration for the Steam worker."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fsm_panel.core import AccountRepository, EffectState, ExternalEffectsLedger

from .ipc import MAX_REQUEST, WorkerClient
from .models import (
    ActivityResult,
    Asset,
    InventoryResult,
    InventorySpec,
    SteamCredentials,
    TradeError,
    TradeResult,
    TradeState,
    WorkerRejected,
    valid_decimal,
    valid_id,
)

# Returns a wall-clock deadline in milliseconds, derived from verified online admission.
# Do not return a constant True. Rechecked during active new work; recovery bypasses it.
AdmissionProvider = Callable[[str], Awaitable[int]]


class TradeService:
    def __init__(
        self,
        repository: AccountRepository,
        worker: WorkerClient,
        admission: AdmissionProvider | None = None,
    ) -> None:
        self.repository, self.worker, self.admission = repository, worker, admission
        self.ledger = ExternalEffectsLedger(repository)
        self._lock = asyncio.Lock()
        self._groups = asyncio.Lock()

    @staticmethod
    def _validate(
        account_id: str,
        credentials: SteamCredentials,
        inventories: Sequence[InventorySpec],
        timeout_ms: int,
        operation_id: str | None = None,
        trade_url: str | None = None,
        allow_partial: bool = True,
    ) -> None:
        if (
            not valid_id(account_id)
            or not isinstance(credentials, SteamCredentials)
            or type(timeout_ms) is not int
            or not 10 <= timeout_ms <= 300_000
            or not isinstance(inventories, Sequence)
            or not 1 <= len(inventories) <= 20
            or any(not isinstance(spec, InventorySpec) for spec in inventories)
            or type(allow_partial) is not bool
        ):
            raise TradeError("invalid_request")
        if operation_id is None:
            return
        if not valid_id(operation_id) or not isinstance(trade_url, str) or len(trade_url) > 2048:
            raise TradeError("invalid_request")
        try:
            url = urlsplit(trade_url)
            query = parse_qs(url.query, keep_blank_values=True)
            if (
                any(ord(char) < 32 for char in trade_url)
                or url.scheme != "https"
                or url.hostname != "steamcommunity.com"
                or url.port not in {None, 443}
                or url.username
                or url.password
                or url.fragment
                or url.path != "/tradeoffer/new/"
                or set(query) - {"partner", "token"}
                or len(query.get("partner", [])) != 1
                or not valid_decimal(query["partner"][0])
                or len(query.get("token", [])) > 1
            ):
                raise ValueError
        except ValueError:
            raise TradeError("invalid_request") from None

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

    @staticmethod
    def _validate_selection(selected_assets: Sequence[Asset] | None, comment: str) -> None:
        if (
            not isinstance(comment, str)
            or len(comment) > 128
            or any(ord(char) < 32 for char in comment)
            or (
                selected_assets is not None
                and (
                    not isinstance(selected_assets, Sequence)
                    or not 1 <= len(selected_assets) <= 500
                    or any(not isinstance(item, Asset) for item in selected_assets)
                )
            )
        ):
            raise TradeError("invalid_request")

    @staticmethod
    def _bound_send(params: dict[str, Any], timeout_ms: int, deadline: int) -> None:
        # Bound the actual ASCII JSON envelope before reserving any durable effect.
        if (
            len(
                json.dumps(
                    {
                        "version": 1,
                        "request_id": "x" * 36,
                        "method": "send",
                        "timeout_ms": timeout_ms,
                        "params": params,
                        "admission": {"expires_at_ms": deadline},
                    }
                ).encode("utf8")
            )
            > MAX_REQUEST
        ):
            raise TradeError("frame_too_large")

    async def _new_work(
        self, method: str, params: dict[str, Any], timeout_ms: int, deadline: int
    ) -> dict[str, Any]:
        task = asyncio.create_task(
            self.worker.request(method, params, timeout_ms=timeout_ms, admitted_until_ms=deadline)
        )
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if not done:
                    refreshed = await self._admit(params["account_id"])
                    renew = getattr(self.worker, "renew_admission", None)
                    if renew:
                        await renew(refreshed)
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError, TradeError:
                    pass

    async def inventory(
        self,
        account_id: str,
        credentials: SteamCredentials,
        inventories: Sequence[InventorySpec] = (InventorySpec(),),
        *,
        timeout_ms: int = 30_000,
    ) -> InventoryResult:
        async with self._lock:
            self._validate(account_id, credentials, inventories, timeout_ms)
            deadline = await self._admit(account_id)
            result = await self._new_work(
                "inventory",
                {
                    "account_id": account_id,
                    "credentials": credentials.payload(),
                    "inventories": [spec.payload() for spec in inventories],
                },
                timeout_ms,
                deadline,
            )
            return InventoryResult.from_payload(result)

    async def send(
        self,
        account_id: str,
        operation_id: str,
        trade_url: str,
        credentials: SteamCredentials,
        inventories: Sequence[InventorySpec] = (InventorySpec(),),
        *,
        allow_partial: bool = True,
        timeout_ms: int = 30_000,
        selected_assets: Sequence[Asset] | None = None,
        comment: str = "",
    ) -> TradeResult:
        async with self._lock:
            self._validate_selection(selected_assets, comment)
            self._validate(
                account_id,
                credentials,
                inventories,
                timeout_ms,
                operation_id,
                trade_url,
                allow_partial,
            )
            deadline = await self._admit(account_id)
            params = {
                "account_id": account_id,
                "operation_id": operation_id,
                "trade_url": trade_url,
                "credentials": credentials.payload(),
                "inventories": [spec.payload() for spec in inventories],
                "allow_partial": allow_partial,
                "comment": comment,
                **(
                    {"selected_assets": [item.payload() for item in selected_assets]}
                    if selected_assets is not None
                    else {}
                ),
            }
            self._bound_send(params, timeout_ms, deadline)
            for prior in self.repository.list_effects():
                if (
                    prior.account_id == account_id
                    and prior.kind == "steam_trade"
                    and prior.idempotency_key != "steam:" + operation_id
                    and prior.state
                    in {EffectState.UNKNOWN, EffectState.PREPARED, EffectState.IN_FLIGHT}
                ):
                    raise TradeError("unresolved_operation")
            # DuplicateEffect is deliberate: calling send again is never a retry path.
            effect = self.ledger.reserve(account_id, "steam_trade", "steam:" + operation_id)
            self.ledger.begin(effect.id)
            try:
                response = await self._new_work(
                    "send",
                    params,
                    timeout_ms,
                    deadline,
                )
                result = self._result(response, account_id, operation_id)
            except TradeError as error:
                if isinstance(error, WorkerRejected) and error.code in {
                    "unresolved_operation",
                    "invalid_request",
                    "frame_too_large",
                    "worker_busy",
                    "admission_required",
                    "admission_expired",
                    "request_limit",
                    "protocol_version",
                }:
                    # The worker's account-level unique guard ran before creation/dispatch.
                    self.ledger.fail_confirmed(effect.id)
                else:
                    self.ledger.mark_unknown(effect.id)
                raise
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

    async def send_grouped(
        self,
        account_id: str,
        operation_id: str,
        trade_url: str,
        credentials: SteamCredentials,
        inventories: Sequence[InventorySpec] = (InventorySpec(),),
        *,
        selected_assets: Sequence[Asset] | None = None,
        comment: str = "",
        confirm_pending: bool = True,
        allow_partial: bool = True,
        timeout_ms: int = 30_000,
    ) -> tuple[TradeResult, ...]:
        """Embedded-v9 grouping, serialized into explicit durable child intents.

        Repeating a batch raises DuplicateEffect; inspect/reconcile its :cs2/:other
        child IDs. No restart path invents another child or replays an unknown offer.
        """
        self._validate(
            account_id, credentials, inventories, timeout_ms, operation_id, trade_url, allow_partial
        )
        self._validate_selection(selected_assets, comment)
        if len(operation_id) > 120 or type(confirm_pending) is not bool:
            raise TradeError("invalid_request")
        if confirm_pending and not credentials.identity_secret:
            raise TradeError("confirmation_unavailable")
        async with self._groups:
            assets = selected_assets
            if assets is None:
                inventory = await self.inventory(
                    account_id, credentials, inventories, timeout_ms=timeout_ms
                )
                if inventory.failures and not allow_partial:
                    raise TradeError("partial_inventory")
                assets = inventory.assets
            if not assets:
                raise TradeError("no_items")
            if len(assets) > 500:
                raise TradeError("frame_too_large")
            groups = (
                (
                    "cs2",
                    tuple(
                        a
                        for a in assets
                        if a.app_id == 730 and (selected_assets is not None or a.context_id == "2")
                    ),
                ),
                (
                    "other",
                    tuple(
                        a
                        for a in assets
                        if a.app_id != 730 or (selected_assets is None and a.context_id != "2")
                    ),
                ),
            )
            deadline = await self._admit(account_id)
            for group, items in groups:
                if items:
                    self._bound_send(
                        {
                            "account_id": account_id,
                            "operation_id": operation_id + ":" + group,
                            "trade_url": trade_url,
                            "credentials": credentials.payload(),
                            "inventories": [spec.payload() for spec in inventories],
                            "allow_partial": allow_partial,
                            "comment": comment if group == "cs2" else "",
                            "selected_assets": [item.payload() for item in items],
                        },
                        timeout_ms,
                        deadline,
                    )
            # Reserve the complete batch identity even when this scope contains only
            # one child. Restarting with a different scope must never add a new child.
            batch = self.ledger.reserve(
                account_id, "steam_trade_group", "steam-group:" + operation_id
            )
            self.ledger.begin(batch.id)
            results = []
            try:
                for group, items in groups:
                    if not items:
                        continue
                    result = await self.send(
                        account_id,
                        operation_id + ":" + group,
                        trade_url,
                        credentials,
                        inventories,
                        selected_assets=items,
                        comment=comment if group == "cs2" else "",
                        allow_partial=allow_partial,
                        timeout_ms=timeout_ms,
                    )
                    if result.state == TradeState.CREATED_PENDING_CONFIRMATION and confirm_pending:
                        result = await self.confirm(
                            account_id, result.operation_id, credentials, timeout_ms=timeout_ms
                        )
                    results.append(result)
                    if result.state not in {TradeState.SENT, TradeState.CONFIRMED}:
                        break
            except BaseException:
                self.ledger.mark_unknown(batch.id)
                raise
            if any(result.state == TradeState.UNKNOWN_OUTCOME for result in results):
                self.ledger.mark_unknown(batch.id)
            elif len(results) == sum(bool(items) for _, items in groups) and all(
                result.state in {TradeState.SENT, TradeState.CONFIRMED} for result in results
            ):
                self.ledger.succeed(batch.id)
            else:
                self.ledger.fail_confirmed(batch.id)
            return tuple(results)

    async def activity(
        self,
        account_id: str,
        operation_id: str,
        credentials: SteamCredentials,
        app_ids: Sequence[int],
        duration_minutes: int,
        machine_name: str = "FSMPanel",
    ) -> ActivityResult:
        """Request free Steam app entitlements, warm up for 80s, then report games played.

        This is Steam activity reporting; it does not launch games, farm items, or
        purchase entitlements. Every request is explicit and licence admitted.
        """
        self._validate(account_id, credentials, (InventorySpec(),), 30_000)
        if (
            not valid_id(operation_id)
            or type(duration_minutes) is not int
            or not 1 <= duration_minutes <= 1440
            or not isinstance(app_ids, Sequence)
            or not 1 <= len(app_ids) <= 50
            or any(type(app) is not int or not 1 <= app <= 2147483647 for app in app_ids)
            or not isinstance(machine_name, str)
            or not 1 <= len(machine_name) <= 128
            or any(ord(char) < 32 for char in machine_name)
        ):
            raise TradeError("invalid_request")
        async with self._lock:
            deadline = await self._admit(account_id)
            for prior in self.repository.list_effects():
                if (
                    prior.account_id == account_id
                    and prior.kind == "steam_activity"
                    and prior.idempotency_key != "activity:" + operation_id
                    and prior.state
                    in {EffectState.PREPARED, EffectState.IN_FLIGHT, EffectState.UNKNOWN}
                ):
                    raise TradeError("unresolved_operation")
            params = {
                "account_id": account_id,
                "operation_id": operation_id,
                "credentials": credentials.payload(),
                "app_ids": list(dict.fromkeys(app_ids)),
                "duration_ms": duration_minutes * 60_000,
                "machine_name": machine_name,
            }
            if len(json.dumps(params).encode()) + 256 > MAX_REQUEST:
                raise TradeError("frame_too_large")
            effect = self.ledger.reserve(account_id, "steam_activity", "activity:" + operation_id)
            self.ledger.begin(effect.id)
            try:
                payload = await self._new_work(
                    "activity", params, duration_minutes * 60_000 + 110_000, deadline
                )
                result = ActivityResult.from_payload(payload)
                if result.operation_id != operation_id or result.account_id != account_id:
                    raise TradeError("invalid_worker_response")
            except BaseException:
                self.ledger.mark_unknown(effect.id)
                raise
            if result.state == "unknown_outcome":
                self.ledger.mark_unknown(effect.id)
            elif result.state == "completed":
                self.ledger.succeed(effect.id)
            else:
                self.ledger.fail_confirmed(effect.id)
            return result

    async def activity_status(self, account_id: str, operation_id: str) -> ActivityResult:
        async with self._lock:
            self.repository.get(account_id)
            payload = await self.worker.request(
                "activity_status", {"account_id": account_id, "operation_id": operation_id}
            )
            result = ActivityResult.from_payload(payload)
            if result.operation_id != operation_id or result.account_id != account_id:
                raise TradeError("invalid_worker_response")
            if result.state in {"completed", "cancelled", "failed"}:
                for effect in self.repository.list_effects():
                    if (
                        effect.kind == "steam_activity"
                        and effect.account_id == account_id
                        and effect.idempotency_key == "activity:" + operation_id
                        and effect.state == EffectState.UNKNOWN
                    ):
                        self.ledger.reconcile(effect.id, succeeded=result.state == "completed")
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
            return self._result(
                await self.worker.request(
                    "status",
                    {
                        "account_id": account_id,
                        "operation_id": operation_id,
                    },
                ),
                account_id,
                operation_id,
            )

    async def confirm(
        self,
        account_id: str,
        operation_id: str,
        credentials: SteamCredentials,
        *,
        timeout_ms: int = 30_000,
    ) -> TradeResult:
        return await self._recovery("confirm", account_id, operation_id, credentials, timeout_ms)

    async def reconcile(
        self,
        account_id: str,
        operation_id: str,
        credentials: SteamCredentials,
        *,
        timeout_ms: int = 30_000,
    ) -> TradeResult:
        return await self._recovery("reconcile", account_id, operation_id, credentials, timeout_ms)

    async def _recovery(
        self,
        method: str,
        account_id: str,
        operation_id: str,
        credentials: SteamCredentials,
        timeout_ms: int,
    ) -> TradeResult:
        async with self._lock:
            self.repository.get(account_id)
            result = self._result(
                await self.worker.request(
                    method,
                    {
                        "account_id": account_id,
                        "operation_id": operation_id,
                        "credentials": credentials.payload(),
                    },
                    timeout_ms=timeout_ms,
                ),
                account_id,
                operation_id,
            )
            if method == "reconcile" and result.state != TradeState.UNKNOWN_OUTCOME:
                for effect in self.repository.list_effects():
                    if (
                        effect.idempotency_key == "steam:" + operation_id
                        and effect.account_id == account_id
                        and effect.state == EffectState.UNKNOWN
                        and result.state != TradeState.NOT_SENT
                    ):
                        self.ledger.reconcile(
                            effect.id, succeeded=result.state != TradeState.FAILED
                        )
            return result
