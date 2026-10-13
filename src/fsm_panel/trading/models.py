"""Public Steam worker types. Credentials are private IPC input, never metadata."""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PUBLIC_CODES = frozenset(
    {
        "invalid_request",
        "protocol_version",
        "invalid_json",
        "frame_too_large",
        "worker_busy",
        "duplicate_request",
        "request_limit",
        "admission_required",
        "admission_expired",
        "cancelled",
        "deadline_exceeded",
        "operation_not_found",
        "operation_conflict",
        "confirmation_unavailable",
        "no_items",
        "inventory_failed",
        "inventory_limit",
        "pagination_loop",
        "invalid_inventory",
        "partial_inventory",
        "auth_failed",
        "interactive_auth_required",
        "cookies_failed",
        "transport_lost",
        "rate_limited",
        "send_unknown",
        "confirmation_unknown",
        "status_unavailable",
        "live_stand_required",
        "internal_error",
        "journal_error",
        "recovered_after_restart",
        "unresolved_operation",
    }
)
_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,128}\Z")
_DECIMAL = re.compile(r"[1-9][0-9]{0,19}\Z")


def valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def valid_decimal(value: object) -> bool:
    return isinstance(value, str) and _DECIMAL.fullmatch(value) is not None


def _integer(value: object, maximum: int) -> bool:
    return type(value) is int and 0 < value <= maximum


class TradeState(StrEnum):
    NOT_SENT = "not_sent"
    CREATED_PENDING_CONFIRMATION = "created_pending_confirmation"
    CONFIRMED = "confirmed"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


class TradeError(Exception):
    """Only a public machine code; never wraps a raw subprocess or SDK error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkerRejected(TradeError):
    """A validated negative IPC response, distinct from timeout or local admission loss."""


@dataclass(frozen=True)
class SteamCredentials:
    account_name: str = field(repr=False)
    password: str = field(repr=False)
    shared_secret: str = field(repr=False)
    identity_secret: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value, limit in (
            (self.account_name, 128),
            (self.password, 1024),
            (self.shared_secret, 1024),
            (self.identity_secret or "unused", 1024),
        ):
            if (
                not isinstance(value, str)
                or not 0 < len(value) <= limit
                or any(ord(char) < 32 for char in value)
            ):
                raise TradeError("invalid_request")

    def payload(self) -> dict[str, str]:
        result = {
            "account_name": self.account_name,
            "password": self.password,
            "shared_secret": self.shared_secret,
        }
        if self.identity_secret is not None:
            result["identity_secret"] = self.identity_secret
        return result


@dataclass(frozen=True)
class InventorySpec:
    app_id: int = 730
    context_id: str = "2"

    def __post_init__(self) -> None:
        if not _integer(self.app_id, 2147483647) or not valid_decimal(self.context_id):
            raise TradeError("invalid_request")

    def payload(self) -> dict[str, int | str]:
        return {"app_id": self.app_id, "context_id": self.context_id}


@dataclass(frozen=True)
class Asset:
    app_id: int
    context_id: str
    asset_id: str
    amount: int

    def __post_init__(self) -> None:
        InventorySpec(self.app_id, self.context_id)
        if not valid_decimal(self.asset_id) or not _integer(self.amount, 2**53 - 1):
            raise TradeError("invalid_worker_response")


@dataclass(frozen=True)
class InventoryFailure:
    app_id: int
    context_id: str
    code: str

    def __post_init__(self) -> None:
        InventorySpec(self.app_id, self.context_id)
        if not isinstance(self.code, str) or self.code not in PUBLIC_CODES:
            raise TradeError("invalid_worker_response")


@dataclass(frozen=True)
class InventoryResult:
    assets: tuple[Asset, ...]
    failures: tuple[InventoryFailure, ...]
    simulation: bool

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> InventoryResult:
        try:
            if (
                set(value) != {"assets", "failures", "simulation"}
                or type(value["simulation"]) is not bool
                or not isinstance(value["assets"], list)
                or len(value["assets"]) > 20_000
                or not isinstance(value["failures"], list)
                or len(value["failures"]) > 20
            ):
                raise ValueError
            return cls(
                tuple(Asset(**item) for item in value["assets"]),
                tuple(InventoryFailure(**item) for item in value["failures"]),
                value["simulation"],
            )
        except KeyError, TypeError, ValueError, TradeError:
            raise TradeError("invalid_worker_response") from None


@dataclass(frozen=True)
class TradeResult:
    operation_id: str
    account_id: str
    state: TradeState
    offer_id: str | None
    code: str | None
    updated_at_ms: int
    simulation: bool

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> TradeResult:
        try:
            result = cls(**{**value, "state": TradeState(value["state"])})
            if (
                type(result.simulation) is not bool
                or type(result.updated_at_ms) is not int
                or not 0 <= result.updated_at_ms <= 2**53 - 1
                or not valid_id(result.account_id)
                or not valid_id(result.operation_id)
                or (result.offer_id is not None and not valid_decimal(result.offer_id))
                or (
                    result.code is not None
                    and (not isinstance(result.code, str) or result.code not in PUBLIC_CODES)
                )
            ):
                raise ValueError
            return result
        except KeyError, TypeError, ValueError:
            raise TradeError("invalid_worker_response") from None
