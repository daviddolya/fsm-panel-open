import pytest

from fsm_panel.core import (
    AccountRepository,
    DuplicateEffect,
    EffectState,
    ExternalEffectsLedger,
    StateConflict,
)


def test_crash_after_send_never_allows_automatic_retry(tmp_path):
    path = tmp_path / "journal.sqlite"
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic_trade")
        ledger = ExternalEffectsLedger(repo)
        effect = ledger.reserve(account.id, "trade", "synthetic_operation_1")
        ledger.begin(effect.id)
        # Simulate response loss/process crash: no recorded successful response.
    with AccountRepository(path) as repo:
        ledger = ExternalEffectsLedger(repo)
        assert ledger.recover() == 1
        assert ledger.recover() == 0
        assert repo.get_effect(effect.id).state == EffectState.UNKNOWN
        with pytest.raises(DuplicateEffect):
            ledger.reserve(account.id, "trade", "synthetic_operation_1")
        with pytest.raises(StateConflict):
            ledger.begin(effect.id)
        assert ledger.reconcile(effect.id, succeeded=True).state == EffectState.SUCCEEDED
        with pytest.raises(DuplicateEffect):
            ledger.reserve(account.id, "trade", "synthetic_operation_1")


def test_effect_claim_is_atomic_and_failed_requires_new_intent():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        ledger = ExternalEffectsLedger(repo)
        first = ledger.reserve(account.id, "trade", "synthetic_key")
        ledger.begin(first.id)
        with pytest.raises(StateConflict):
            ledger.begin(first.id)
        assert ledger.fail_confirmed(first.id).state == EffectState.FAILED_CONFIRMED
        with pytest.raises(DuplicateEffect):
            ledger.reserve(account.id, "trade", "synthetic_key")
        second = ledger.reserve(account.id, "trade", "synthetic_new_explicit_intent")
        ledger.begin(second.id)
        assert ledger.succeed(second.id).state == EffectState.SUCCEEDED


def test_prepared_effect_is_also_unknown_on_recovery():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        effect = repo.reserve_effect(account.id, "trade", "synthetic")
        repo.recover_effects()
        assert repo.get_effect(effect.id).state == EffectState.UNKNOWN


def test_explicit_unknown_can_only_be_reconciled():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        ledger = ExternalEffectsLedger(repo)
        effect = ledger.reserve(account.id, "trade", "synthetic")
        ledger.begin(effect.id)
        ledger.mark_unknown(effect.id)
        with pytest.raises(StateConflict):
            ledger.succeed(effect.id)
        assert ledger.reconcile(effect.id, succeeded=False).state == EffectState.FAILED_CONFIRMED
