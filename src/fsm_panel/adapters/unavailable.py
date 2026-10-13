"""Fail-closed defaults. No native game success is claimed without evidence."""

from fsm_panel.core.models import Account, Admission, UnavailableError


class DenyLease:
    async def admit(self, account_id: str) -> Admission:
        return Admission(False, "license_not_configured")


class NativeGameUnavailable:
    reason = "Native Panel/game behavior has not been verified; real automation is unavailable."

    async def launch(self, account: Account) -> None:
        raise UnavailableError("native_behavior_unverified")

    async def pause(self, account: Account) -> None:
        raise UnavailableError("native_behavior_unverified")

    async def resume(self, account: Account) -> None:
        raise UnavailableError("native_behavior_unverified")

    async def stop(self, account: Account) -> None:
        # This adapter cannot launch or own any process; there is nothing to terminate.
        return None

    async def recover(self, account: Account) -> None:
        return None
