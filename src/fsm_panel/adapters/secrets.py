"""Current-user DPAPI secrets. Unsupported platforms fail closed unless a test seam is injected."""

import os
import re
import threading
from pathlib import Path

from fsm_panel.core.contracts import SecretProtector
from fsm_panel.core.models import UnavailableError, ValidationError
from fsm_panel.core.settings import atomic_write

_REFERENCE = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")


class DPAPIProtector:
    """Uses Windows current-user scope and UI-forbidden protection; no plaintext fallback."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise UnavailableError("dpapi_requires_windows")

    def protect(self, plaintext: bytes) -> bytes:
        try:
            import win32crypt

            return win32crypt.CryptProtectData(plaintext, "FSM Panel Open", None, None, None, 1)
        except Exception:
            raise UnavailableError("secret_protection_failed") from None

    def unprotect(self, ciphertext: bytes) -> bytes:
        try:
            import win32crypt

            _, plaintext = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 1)
            return plaintext
        except Exception:
            raise UnavailableError("secret_unprotection_failed") from None


class DPAPISecretStore:
    """Files contain only DPAPI ciphertext; references are opaque, path-safe identifiers.

    `protector` is an explicit seam for portable tests, not a production fallback.
    Windows account access controls still matter: same-user malware can call DPAPI.
    """

    def __init__(self, directory: str | Path, *, protector: SecretProtector | None = None) -> None:
        self.directory = Path(directory)
        self._protector = protector if protector is not None else DPAPIProtector()
        self._lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return "DPAPISecretStore(protected=True)"

    def _path(self, reference: str) -> Path:
        if not _REFERENCE.fullmatch(reference):
            raise ValidationError("invalid_secret_reference")
        return self.directory / f"{reference}.dpapi"

    def put(self, reference: str, value: bytes) -> None:
        path = self._path(reference)
        if type(value) is not bytes or not value or len(value) > 1024 * 1024:
            raise ValidationError("invalid_secret_value")
        try:
            encrypted = self._protector.protect(value)
            if not encrypted or encrypted == value:
                raise ValidationError("secret_protection_invalid")
            with self._lock:
                atomic_write(path, encrypted)
        except ValidationError:
            raise
        except Exception:
            raise UnavailableError("secret_write_failed") from None

    def get(self, reference: str) -> bytes:
        path = self._path(reference)
        try:
            with self._lock:
                raw = path.read_bytes()
            return self._protector.unprotect(raw)
        except Exception:
            raise UnavailableError("secret_read_failed") from None

    def delete(self, reference: str) -> None:
        path = self._path(reference)
        try:
            with self._lock:
                path.unlink(missing_ok=True)
        except OSError:
            raise UnavailableError("secret_delete_failed") from None
