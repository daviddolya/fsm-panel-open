"""Application settings persistence with encrypted secret references.

Legacy SettingsStore import/export remains explicit plaintext JSON compatibility.
This envelope is for the new application's private settings file, not the legacy app.
"""

import copy
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from fsm_panel.core.contracts import SecretStore
from fsm_panel.core.models import ValidationError
from fsm_panel.core.settings import (
    MAX_SETTINGS_BYTES,
    SettingsDocument,
    _is_sensitive,
    _object_pairs,
    _validate_json,
    atomic_write,
)

_FORMAT = "fsm-panel-settings-v2"
_LEGACY_FORMAT = "fsm-panel-settings-v1"
_VAULT_LOCK = threading.RLock()


def _was_sensitive_v1(key: str) -> bool:
    """Frozen historical policy for a read-only v1 compatibility import."""
    return key in {"TG_BOT_API_KEY", "TG_BOT_CHAT_ID", "TRADEOFFER_LINK"} or any(
        word in key.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CREDENTIAL")
    )


class SettingsVault:
    """Inject DPAPISecretStore in production; MemorySecretStore only in tests.

    Each save uses fresh references, so old backups remain decryptable. References
    are deliberately retained even after a failed save; automatic deletion after an
    uncertain file replace could destroy a committed settings version. No GC is run.
    """

    def __init__(self, secrets: SecretStore) -> None:
        self.secrets = secrets

    def __repr__(self) -> str:
        return "SettingsVault(protected=True)"

    def _encode(self, document: SettingsDocument) -> bytes:
        references: list[dict[str, Any]] = []

        def seal(value: Any, location: list[str | int]) -> Any:
            if type(value) is dict:
                result: dict[str, Any] = {}
                for key, item in value.items():
                    child_path = [*location, key]
                    if _is_sensitive(key):
                        reference = f"settings-{uuid.uuid4()}"
                        raw = json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
                        self.secrets.put(reference, raw)
                        references.append({"path": child_path, "reference": reference})
                        result[key] = None
                    else:
                        result[key] = seal(item, child_path)
                return result
            if type(value) is list:
                return [seal(item, [*location, index]) for index, item in enumerate(value)]
            return copy.deepcopy(value)

        try:
            values = seal(document.to_dict(include_secrets=True), [])
            envelope = {"format": _FORMAT, "values": values, "secrets": references}
            raw = (
                json.dumps(envelope, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            ).encode("utf-8")
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValidationError("settings_vault_too_large")
            return raw
        except Exception:
            raise ValidationError("settings_vault_save_failed") from None

    def save(self, document: SettingsDocument, path: str | Path, *, backup: bool = True) -> None:
        try:
            raw = self._encode(document)
            target = Path(path)
            with _VAULT_LOCK:
                if backup and target.exists():
                    # Only vault envelopes can be backed up here; importing a legacy
                    # plaintext file must save to a distinct application destination.
                    previous = self.load(target)
                    previous_format = self._read_envelope(target)["format"]
                    backup_raw = (
                        self._encode(previous)
                        if previous_format == _LEGACY_FORMAT
                        else target.read_bytes()
                    )
                    atomic_write(target.with_suffix(target.suffix + ".bak"), backup_raw)
                atomic_write(target, raw)
        except Exception:
            raise ValidationError("settings_vault_save_failed") from None

    @staticmethod
    def _read_envelope(path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValidationError("settings_vault_too_large")
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs)
            _validate_json(envelope, depth=-2)
            if (
                type(envelope) is not dict
                or set(envelope) != {"format", "values", "secrets"}
                or envelope["format"] not in {_FORMAT, _LEGACY_FORMAT}
                or type(envelope["values"]) is not dict
                or type(envelope["secrets"]) is not list
            ):
                raise ValidationError("invalid_settings_vault")
            return envelope
        except Exception:
            raise ValidationError("settings_vault_read_failed") from None

    def load(self, path: str | Path) -> SettingsDocument:
        try:
            with _VAULT_LOCK:
                envelope = self._read_envelope(Path(path))
            values = envelope["values"]
            legacy = envelope["format"] == _LEGACY_FORMAT
            seen: set[tuple[str | int, ...]] = set()
            for entry in envelope["secrets"]:
                if (
                    type(entry) is not dict
                    or set(entry) != {"path", "reference"}
                    or type(entry["path"]) is not list
                    or not entry["path"]
                    or any(type(part) not in {str, int} for part in entry["path"])
                    or type(entry["reference"]) is not str
                    or not entry["reference"].startswith("settings-")
                ):
                    raise ValidationError("invalid_settings_secret_reference")
                location = entry["path"]
                if tuple(location) in seen:
                    raise ValidationError("duplicate_settings_secret_reference")
                seen.add(tuple(location))
                parent = values
                for part in location[:-1]:
                    if type(part) is int and (type(parent) is not list or part < 0):
                        raise ValidationError("invalid_settings_secret_path")
                    if type(part) is str and type(parent) is not dict:
                        raise ValidationError("invalid_settings_secret_path")
                    parent = parent[part]
                final = location[-1]
                if type(final) is not str or type(parent) is not dict or not _is_sensitive(final):
                    raise ValidationError("invalid_settings_secret_path")
                if final not in parent or parent[final] is not None:
                    raise ValidationError("invalid_settings_secret_placeholder")
                plaintext = self.secrets.get(entry["reference"])
                secret_value = json.loads(
                    plaintext.decode("utf-8"), object_pairs_hook=_object_pairs
                )
                _validate_json(secret_value)
                parent[final] = secret_value

            # Sensitive fields must have an encrypted reference even if empty or null;
            # this prevents loading a tampered envelope that embeds plaintext secrets.
            def check(value: Any, location: list[str | int]) -> None:
                if type(value) is dict:
                    for key, item in value.items():
                        child = [*location, key]
                        if _is_sensitive(key):
                            if tuple(child) not in seen and (not legacy or _was_sensitive_v1(key)):
                                raise ValidationError("unprotected_settings_secret")
                        else:
                            check(item, child)
                elif type(value) is list:
                    for index, item in enumerate(value):
                        check(item, [*location, index])

            check(values, [])
            return SettingsDocument(values)
        except Exception:
            raise ValidationError("settings_vault_load_failed") from None

    def restore_backup(self, path: str | Path) -> SettingsDocument:
        target = Path(path)
        backup = target.with_suffix(target.suffix + ".bak")
        document = self.load(backup)
        self.save(document, target, backup=False)
        return document
