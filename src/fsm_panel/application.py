"""Composition boundary between core admission, online licenses and Qt."""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from fsm_panel.core import Coordinator
from fsm_panel.core.models import Admission
from fsm_panel.licensing import LicenseClient, LicenseUnavailable


class LicenseAdmission:
    def __init__(self, client: LicenseClient) -> None:
        self.client = client

    async def admit(self, account_id: str) -> Admission:
        try:
            self.client.gate.require_valid()
        except LicenseUnavailable:
            return Admission(False, "license_unavailable")
        return Admission(True, "online_license")


class LicenseRuntime:
    """Losing a lease stops active work; cleanup never requires a valid lease."""

    def __init__(
        self, client: LicenseClient, coordinator: Coordinator, notify: Callable[[str], None]
    ) -> None:
        self.client, self.coordinator, self.notify = client, coordinator, notify
        self._stop = asyncio.Event()
        self._renewal: asyncio.Task | None = None
        self._runner: asyncio.Task | None = None

    async def run(self) -> None:
        self._runner = asyncio.current_task()
        self._renewal = asyncio.create_task(self.client.run_renewal(self._stop))
        last = ""
        try:
            while not self._stop.is_set():
                try:
                    claims = self.client.gate.require_valid()
                    status = f"Онлайн-допуск активен · лимит устройств: {claims.device_limit}"
                except LicenseUnavailable:
                    status = "Нет действующего онлайн-допуска; новые операции запрещены"
                    for account in self.coordinator.repository.list_accounts():
                        if account.state.value in {"starting", "running", "paused"}:
                            await self.coordinator.stop(account.id)
                if status != last:
                    self.notify(status)
                    last = status
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            if self._renewal:
                self._renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await self._renewal

    async def activate(self, key: str) -> None:
        await self.client.activate(key)
        self.notify("Активация выполнена; онлайн-допуск получен")

    async def close(self) -> None:
        self._stop.set()
        if self._runner and self._runner is not asyncio.current_task():
            await self._runner
        await self.client.aclose()
