"""Authenticated, bounded, observation-only Game State Integration storage."""

import copy
import hashlib
import hmac
import json
import math
import threading
from dataclasses import dataclass, field
from typing import Any

from fsm_panel.core.clock import RealClock
from fsm_panel.core.contracts import Clock

MAX_CLIENTS = 50
DEFAULT_MAX_PAYLOAD_BYTES = 256 * 1024
DEFAULT_MAX_DEPTH = 32


class GsiError(Exception):
    """Only stable public codes; never include input or upstream exception text."""

    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GameStateSnapshot:
    account_id: str
    steam_id: str
    received_at: float
    payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Registration:
    account_id: str
    steam_id: str
    token_digest: bytes = field(repr=False)


def _steam_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= len(value) <= 20
        or value.startswith("0")
        or int(value) > (1 << 64) - 1
    ):
        raise GsiError("invalid_steam_id")
    return value


def _token_digest(value: object) -> bytes:
    if not isinstance(value, str) or not 32 <= len(value) <= 256 or not value.isascii():
        raise GsiError("invalid_token")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise GsiError("invalid_token")
    return hashlib.sha256(value.encode("ascii")).digest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non_finite_number")


def _check_depth(text: str, maximum: int) -> None:
    # Bound nesting before json.loads allocates a recursive object tree.
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                raise GsiError("payload_too_deep", 413)
        elif character in "]}":
            depth -= 1


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redacted(item)
            for key, item in value.items()
            if key.casefold() not in {"auth", "token"}
        }
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise GsiError("invalid_payload")
    return value


class GameStateReceiver:
    """Keep the last authenticated arrival for each explicitly registered account.

    Registration, receive, and snapshots are safe across threads. Returned payloads
    are independent copies. This class has no launch, game, lease, or persistence port.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_clients: int = MAX_CLIENTS,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        if type(max_clients) is not int or not 1 <= max_clients <= MAX_CLIENTS:
            raise ValueError("invalid_max_clients")
        if type(max_payload_bytes) is not int or max_payload_bytes < 1:
            raise ValueError("invalid_max_payload_bytes")
        if type(max_depth) is not int or not 1 <= max_depth <= 64:
            raise ValueError("invalid_max_depth")
        self._clock = clock or RealClock()
        self._max_clients = max_clients
        self._max_payload_bytes = max_payload_bytes
        self._max_depth = max_depth
        self._registrations: dict[str, _Registration] = {}
        self._steam_accounts: dict[str, str] = {}
        self._latest: dict[str, GameStateSnapshot] = {}
        self._lock = threading.Lock()

    @property
    def max_payload_bytes(self) -> int:
        return self._max_payload_bytes

    def register(self, account_id: str, steam_id: str, token: str) -> None:
        """Register one identity and unique token; unregister before replacing it."""
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= 128:
            raise GsiError("invalid_account_id")
        steam_id = _steam_id(steam_id)
        digest = _token_digest(token)
        with self._lock:
            if account_id in self._registrations or steam_id in self._steam_accounts:
                raise GsiError("registration_exists", 409)
            if any(
                hmac.compare_digest(digest, registration.token_digest)
                for registration in self._registrations.values()
            ):
                raise GsiError("token_already_registered", 409)
            if len(self._registrations) >= self._max_clients:
                raise GsiError("client_limit_reached", 409)
            self._registrations[account_id] = _Registration(account_id, steam_id, digest)
            self._steam_accounts[steam_id] = account_id

    def unregister(self, account_id: str) -> None:
        """Revoke reception and forget the account's telemetry; idempotent."""
        with self._lock:
            registration = self._registrations.pop(account_id, None)
            if registration is not None:
                self._steam_accounts.pop(registration.steam_id, None)
            self._latest.pop(account_id, None)

    def snapshot(self, account_id: str) -> GameStateSnapshot | None:
        with self._lock:
            return copy.deepcopy(self._latest.get(account_id))

    def receive(self, body: bytes) -> GameStateSnapshot:
        """Validate a UTF-8 JSON body and record its local monotonic arrival time."""
        if not isinstance(body, bytes):
            raise GsiError("invalid_payload")
        if len(body) > self._max_payload_bytes:
            raise GsiError("payload_too_large", 413)
        try:
            text = body.decode("utf-8")
            _check_depth(text, self._max_depth)
            payload = json.loads(
                text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
        except UnicodeDecodeError, ValueError, RecursionError:
            raise GsiError("invalid_payload") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("provider"), dict):
            raise GsiError("invalid_payload")
        steam_id = _steam_id(payload["provider"].get("steamid"))
        auth = payload.get("auth")
        try:
            digest = _token_digest(auth.get("token") if isinstance(auth, dict) else None)
        except GsiError:
            raise GsiError("unauthorized", 401) from None
        with self._lock:
            account_id = self._steam_accounts.get(steam_id)
            registration = self._registrations.get(account_id) if account_id is not None else None
            expected = registration.token_digest if registration else bytes(32)
            matches = hmac.compare_digest(digest, expected)
            if registration is None or not matches:
                raise GsiError("unauthorized", 401)
            # Remove authentication recursively before the first stored snapshot.
            safe_payload = _redacted(payload)
            try:
                received_at = float(self._clock.monotonic())
                if not math.isfinite(received_at):
                    raise ValueError("invalid_clock")
            except Exception:
                raise GsiError("clock_unavailable", 503) from None
            snapshot = GameStateSnapshot(
                registration.account_id, registration.steam_id, received_at, safe_payload
            )
            self._latest[registration.account_id] = snapshot
            return copy.deepcopy(snapshot)
