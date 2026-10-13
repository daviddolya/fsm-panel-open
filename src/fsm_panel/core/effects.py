"""Durable at-most-once admission for external effects such as future trades."""

from .models import Effect, EffectState
from .repository import AccountRepository


class ExternalEffectsLedger:
    def __init__(self, repository: AccountRepository) -> None:
        self.repository = repository

    def reserve(self, account_id: str, kind: str, idempotency_key: str) -> Effect:
        return self.repository.reserve_effect(account_id, kind, idempotency_key)

    def begin(self, effect_id: str) -> Effect:
        return self.repository.transition_effect(
            effect_id, EffectState.PREPARED, EffectState.IN_FLIGHT
        )

    def succeed(self, effect_id: str) -> Effect:
        return self.repository.transition_effect(
            effect_id, EffectState.IN_FLIGHT, EffectState.SUCCEEDED
        )

    def fail_confirmed(self, effect_id: str) -> Effect:
        return self.repository.transition_effect(
            effect_id, EffectState.IN_FLIGHT, EffectState.FAILED_CONFIRMED
        )

    def mark_unknown(self, effect_id: str) -> Effect:
        return self.repository.transition_effect(
            effect_id, EffectState.IN_FLIGHT, EffectState.UNKNOWN
        )

    def reconcile(self, effect_id: str, *, succeeded: bool) -> Effect:
        """Use only after an authoritative external status check; never resubmits."""
        return self.repository.transition_effect(
            effect_id,
            EffectState.UNKNOWN,
            EffectState.SUCCEEDED if succeeded else EffectState.FAILED_CONFIRMED,
        )

    def recover(self) -> int:
        return self.repository.recover_effects()
