from dataclasses import dataclass
from typing import Protocol


class LicenseUnavailable(RuntimeError):
    """Admission is denied. Recovery, stop and export must still be offered."""


@dataclass(frozen=True)
class LeaseClaims:
    external_id: str
    activation_id: str
    installation_id: str
    device_key_sha256: str
    device_limit: int
    revision: int
    issued_at: int
    expires_at: int
    request_nonce: str


class LeaseGate(Protocol):
    def require_valid(self) -> LeaseClaims:
        """Return the current lease or raise LicenseUnavailable, without network I/O."""
        ...
