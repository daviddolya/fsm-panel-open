"""Portable device-store contract with Windows user-scope DPAPI persistence."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fsm_license.crypto import b64decode, b64encode, canonical_json


class DeviceStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    installation_id: str
    private_key: bytes = field(repr=False)
    activation_id: str | None = None

    @classmethod
    def generate(cls) -> DeviceIdentity:
        return cls(str(uuid4()), Ed25519PrivateKey.generate().private_bytes_raw())

    @property
    def public_key(self) -> bytes:
        return (
            Ed25519PrivateKey.from_private_bytes(self.private_key).public_key().public_bytes_raw()
        )


class DeviceStore(Protocol):
    def load(self) -> DeviceIdentity | None: ...
    def save(self, identity: DeviceIdentity) -> None: ...


class MemoryDeviceStore:
    """Explicitly ephemeral store, intended for tests or one-session tooling."""

    def __init__(self, identity: DeviceIdentity | None = None) -> None:
        self.identity = identity

    def load(self) -> DeviceIdentity | None:
        return self.identity

    def save(self, identity: DeviceIdentity) -> None:
        self.identity = identity


class WindowsDpapiDeviceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _dpapi():
        if os.name != "nt":
            raise DeviceStoreError("Windows DPAPI is unavailable on this platform")
        import win32crypt

        return win32crypt

    def load(self) -> DeviceIdentity | None:
        if not self.path.exists():
            return None
        try:
            _, plaintext = self._dpapi().CryptUnprotectData(
                self.path.read_bytes(), None, None, None, 0x01
            )
            item = json.loads(plaintext)
            if item["version"] != 1:
                raise ValueError("unsupported device format")
            private = b64decode(item["private_key"])
            Ed25519PrivateKey.from_private_bytes(private)
            installation_id = str(UUID(item["installation_id"]))
            activation_id = str(UUID(item["activation_id"])) if item["activation_id"] else None
            return DeviceIdentity(installation_id, private, activation_id)
        except Exception as exc:
            raise DeviceStoreError(
                "device identity could not be decrypted; preserve it for recovery"
            ) from exc

    def save(self, identity: DeviceIdentity) -> None:
        temporary: str | None = None
        try:
            plaintext = canonical_json(
                {
                    "version": 1,
                    "installation_id": identity.installation_id,
                    "private_key": b64encode(identity.private_key),
                    "activation_id": identity.activation_id,
                }
            )
            encrypted = self._dpapi().CryptProtectData(
                plaintext, "FSM Panel device identity", None, None, None, 0x01
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as output:
                temporary = output.name
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except Exception as exc:
            raise DeviceStoreError("device identity could not be saved securely") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
