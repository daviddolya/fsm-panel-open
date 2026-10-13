"""Immutable domain values. No credentials belong in these objects or events."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AccountState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


ACTIVE_STATES = frozenset(
    {AccountState.STARTING, AccountState.RUNNING, AccountState.PAUSED, AccountState.STOPPING}
)


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    login: str = field(repr=False)
    display_name: str = field(default="", repr=False)
    state: AccountState = AccountState.IDLE
    secret_ref: str | None = field(default=None, repr=False)
    error_code: str | None = None
    revision: int = 0


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    account_id: str
    kind: str
    state: AccountState
    code: str | None
    created_at: str
    # Event data is an immutable tuple, intentionally excluding exception strings.
    data: tuple[tuple[str, str | int | bool], ...] = ()


@dataclass(frozen=True, slots=True)
class Admission:
    allowed: bool
    reason: str = ""


class EffectState(StrEnum):
    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED_CONFIRMED = "failed_confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Effect:
    id: str
    account_id: str
    kind: str
    idempotency_key: str = field(repr=False)
    state: EffectState
    created_at: str
    updated_at: str


class CoreError(Exception):
    """Messages are stable codes, never upstream exceptions or credential material."""


class ValidationError(CoreError):
    pass


class StateConflict(CoreError):
    pass


class UnavailableError(CoreError):
    pass


class TransientError(CoreError):
    """A failure confirmed by the adapter to be safe to retry after cleanup."""


class DuplicateEffect(CoreError):
    pass


JsonValue = None | bool | int | float | str | list[Any] | dict[str, Any]
