"""Explicit server configuration; production never gets generated/default secrets."""

import base64
import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    internal_token: str = field(repr=False)
    signing_key: bytes = field(repr=False)
    issuer: str = "fsm-license"
    audience: str = "fsm-panel"
    key_id: str = "license-v1"
    lease_seconds: int = 60
    proof_skew_seconds: int = 30

    def __post_init__(self) -> None:
        if len(self.internal_token) < 32:
            raise ValueError("internal token must contain at least 32 characters")
        Ed25519PrivateKey.from_private_bytes(self.signing_key)
        if not 1 <= self.lease_seconds <= 60:
            raise ValueError("lease duration must be between 1 and 60 seconds")
        if not 1 <= self.proof_skew_seconds <= 30:
            raise ValueError("proof timestamp tolerance must be between 1 and 30 seconds")
        if not self.issuer or not self.audience or not self.key_id:
            raise ValueError("issuer, audience and key ID must be nonempty")

    @classmethod
    def from_env(cls) -> Settings:
        def required(name: str) -> str:
            value = os.environ.get(name, "")
            if not value or value.startswith("REPLACE_"):
                raise ValueError(f"{name} must be explicitly configured")
            return value

        encoded = required("FSM_LICENSE_SIGNING_KEY")
        database_url = required("FSM_LICENSE_DATABASE_URL")
        if not database_url.startswith("postgresql+psycopg://"):
            raise ValueError("production requires a postgresql+psycopg database URL")
        return cls(
            database_url=database_url,
            internal_token=required("FSM_LICENSE_INTERNAL_TOKEN"),
            signing_key=base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)),
            issuer=required("FSM_LICENSE_ISSUER"),
            audience=required("FSM_LICENSE_AUDIENCE"),
            key_id=required("FSM_LICENSE_KEY_ID"),
        )
