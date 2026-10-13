"""Verify pinned keys and bind fresh online responses to monotonic admission deadlines."""

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from fsm_license.crypto import b64decode

from .models import LeaseClaims, LicenseUnavailable


@dataclass(frozen=True)
class _Accepted:
    claims: LeaseClaims
    deadline: float
    accepted_monotonic: float
    accepted_wall: float


class LocalLeaseGate:
    def __init__(
        self,
        *,
        public_keys: Mapping[str, bytes],
        issuer: str,
        audience: str,
        installation_id: str,
        device_key_sha256: str,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keys = dict(public_keys)
        self._issuer = issuer
        self._audience = audience
        self._installation_id = installation_id
        self._fingerprint = device_key_sha256
        self._wall = wall_clock
        self._monotonic = monotonic_clock
        self._accepted: _Accepted | None = None
        self._reason = "online activation or renewal required"
        self._lock = threading.RLock()
        self._consumed_nonces: dict[str, float] = {}

    def invalidate(self, reason: str = "online license verification failed") -> None:
        with self._lock:
            self._accepted = None
            self._reason = reason

    def accept(
        self,
        token: str,
        *,
        activation_id: str,
        request_nonce: str,
        request_started_monotonic: float,
    ) -> LeaseClaims:
        with self._lock:
            try:
                claims = self._verify(token, activation_id, request_nonce)
                now_mono, now_wall = self._monotonic(), self._wall()
                self._consumed_nonces = {
                    nonce: deadline
                    for nonce, deadline in self._consumed_nonces.items()
                    if deadline >= now_mono
                }
                elapsed = now_mono - request_started_monotonic
                if not 0 <= elapsed <= 15:
                    raise ValueError("online response exceeded its deadline")
                if abs(now_wall - claims.issued_at) > 30:
                    raise ValueError("device clock differs from license service")
                if request_nonce in self._consumed_nonces:
                    raise ValueError("lease response already consumed")
                # Starting TTL at request dispatch is conservative despite network latency.
                deadline = min(
                    request_started_monotonic + claims.expires_at - claims.issued_at,
                    now_mono + claims.expires_at - now_wall,
                )
                if deadline <= now_mono:
                    raise ValueError("lease expired")
                self._consumed_nonces[request_nonce] = request_started_monotonic + 15
                # Gate state is never persisted: restart always requires a fresh online response.
                self._accepted = _Accepted(claims, deadline, now_mono, now_wall)
                self._reason = ""
                return claims
            except (ValueError, TypeError, KeyError, InvalidSignature, UnicodeError) as exc:
                self.invalidate("invalid license lease")
                raise LicenseUnavailable("invalid license lease") from exc

    def _verify(self, token: str, activation_id: str, request_nonce: str) -> LeaseClaims:
        if len(token) > 16384:
            raise ValueError("lease too large")
        header_text, payload_text, signature = token.split(".")
        header = json.loads(b64decode(header_text))
        if set(header) != {"alg", "typ", "kid"} or header["alg"] != "EdDSA":
            raise ValueError("invalid lease algorithm")
        if header["typ"] != "JWT" or header["kid"] not in self._keys:
            raise ValueError("untrusted lease key")
        Ed25519PublicKey.from_public_bytes(self._keys[header["kid"]]).verify(
            b64decode(signature), f"{header_text}.{payload_text}".encode("ascii")
        )
        item = json.loads(b64decode(payload_text))
        if item["v"] != 1 or item["iss"] != self._issuer or item["aud"] != self._audience:
            raise ValueError("invalid lease scope")
        if (
            item["activation_id"] != activation_id
            or item["installation_id"] != self._installation_id
            or item["device_key_sha256"] != self._fingerprint
            or item["request_nonce"] != request_nonce
        ):
            raise ValueError("invalid lease binding")
        for field in ("iat", "exp", "device_limit", "revision"):
            if type(item[field]) is not int:
                raise ValueError("invalid numeric claim")
        if not 0 < item["exp"] - item["iat"] <= 60:
            raise ValueError("invalid lease duration")
        if item["device_limit"] < 1 or item["revision"] < 1:
            raise ValueError("invalid entitlement claim")
        if not isinstance(item["sub"], str) or not item["sub"]:
            raise ValueError("invalid license subject")
        return LeaseClaims(
            external_id=item["sub"],
            activation_id=item["activation_id"],
            installation_id=item["installation_id"],
            device_key_sha256=item["device_key_sha256"],
            device_limit=item["device_limit"],
            revision=item["revision"],
            issued_at=item["iat"],
            expires_at=item["exp"],
            request_nonce=item["request_nonce"],
        )

    def require_valid(self) -> LeaseClaims:
        with self._lock:
            state = self._accepted
            if state is None:
                raise LicenseUnavailable(self._reason)
            now_mono, now_wall = self._monotonic(), self._wall()
            elapsed = now_mono - state.accepted_monotonic
            if elapsed < 0 or now_wall < state.accepted_wall + elapsed - 2:
                self.invalidate("device clock moved backwards; online renewal required")
            elif now_mono >= state.deadline or now_wall >= state.claims.expires_at:
                self.invalidate("license lease expired; online renewal required")
            else:
                return state.claims
            raise LicenseUnavailable(self._reason)
