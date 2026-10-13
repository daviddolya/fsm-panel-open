"""Stable public core API for desktop composition and integration tests."""

from .coordinator import Coordinator, RetryPolicy
from .effects import ExternalEffectsLedger
from .importing import ImportResult, import_accounts
from .models import (
    ACTIVE_STATES,
    Account,
    AccountState,
    Admission,
    CoreError,
    DuplicateEffect,
    Effect,
    EffectState,
    Event,
    StateConflict,
    TransientError,
    UnavailableError,
    ValidationError,
)
from .repository import AccountRepository
from .settings import SECRET_KEYS, SETTINGS_TYPES, SettingsDocument, SettingsStore

__all__ = [
    "ACTIVE_STATES", "Account", "AccountRepository", "AccountState", "Admission",
    "Coordinator", "CoreError", "DuplicateEffect", "Effect", "EffectState", "Event",
    "ExternalEffectsLedger", "ImportResult", "RetryPolicy", "SECRET_KEYS", "SETTINGS_TYPES",
    "SettingsDocument", "SettingsStore", "StateConflict", "TransientError", "UnavailableError",
    "ValidationError", "import_accounts",
]
