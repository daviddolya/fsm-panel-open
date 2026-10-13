"""Public Steam worker types. Credentials are private IPC input, never metadata."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True)
class SteamCredentials:
    account_name: str = field(repr=False)
    password: str = field(repr=False)
    shared_secret: str = field(repr=False)
    identity_secret: str | None = field(default=None, repr=False)

    def payload(self) -> dict[str, str]:
        result = {"account_name": self.account_name, "password": self.password,
                  "shared_secret": self.shared_secret}
        if self.identity_secret is not None:
            result["identity_secret"] = self.identity_secret
        return result


@dataclass(frozen=True)
class InventorySpec:
    app_id: int = 730
    context_id: str = "2"

    def payload(self) -> dict[str, int | str]:
        return {"app_id": self.app_id, "context_id": self.context_id}


@dataclass(frozen=True)
class Asset:
    app_id: int
    context_id: str
    asset_id: str
    amount: int


@dataclass(frozen=True)
class InventoryFailure:
    app_id: int
    context_id: str
    code: str


@dataclass(frozen=True)
class InventoryResult:
    assets: tuple[Asset, ...]
    failures: tuple[InventoryFailure, ...]
    simulation: bool

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> InventoryResult:
        try:
            if type(value["simulation"]) is not bool:
                raise ValueError
            return cls(tuple(Asset(**item) for item in value["assets"]),
                       tuple(InventoryFailure(**item) for item in value["failures"]),
                       value["simulation"])
        except (KeyError, TypeError, ValueError):
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
            if (type(result.simulation) is not bool or type(result.updated_at_ms) is not int
                    or not isinstance(result.account_id, str)
                    or not isinstance(result.operation_id, str)
                    or (result.offer_id is not None and (
                        not isinstance(result.offer_id, str) or not result.offer_id.isdecimal()
                        or len(result.offer_id) > 20))):
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError):
            raise TradeError("invalid_worker_response") from None
