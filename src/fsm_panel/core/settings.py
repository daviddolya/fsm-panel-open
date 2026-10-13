"""Lossless JSON settings, with an observed schema and explicit application defaults.

The 49 names/types were observed in the supplied 3.1.2 settings file. Defaults below
are new safe application defaults, not claims about legacy defaults or semantics.
"""

import copy
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .models import ValidationError

SETTINGS_TYPES: dict[str, type] = {
    "ACCOUNT_LOGIN_ATTEMPTS": int,
    "ACCOUNTS_LAUNCH_DELAY": int,
    "ACCOUNTS_SORT_MODE": int,
    "ADDITIONAL_LAUNCH_OPTIONS": str,
    "ALTERNATIVE_AUTOACCEPTER": bool,
    "ANCIENT_MAP_PRELOAD": bool,
    "ANTI_AFK": bool,
    "ANTI_AFK_DELAY": int,
    "ANTI_AFK_MINIMIZE": bool,
    "ANTI_AFK_POWER": int,
    "AUTO_LOOT": bool,
    "AUTOACCEPT_READ_TIME": int,
    "AUTODISCONNECTS": bool,
    "AUTODISCONNECTS_CHANGE_TEAMS_DELAY": int,
    "AUTODISCONNECTS_DELAY": int,
    "AUTOSHUFFLE_AFTER_GAME": bool,
    "AVASTSANDBOX_FOLDER": str,
    "BES_REDUCTION_PERCENT": int,
    "CHANGE_BATCH_AFTER_N_MINUTES": int,
    "CS_RESOLUTION": str,
    "CS2_AUTOUPDATER_ENABLED": bool,
    "CSGO_PATH": str,
    "DELAY_BETWEEN_TRADES": int,
    "DISABLE_CS2_BACKGROUND": bool,
    "DISABLE_STEAM_OVERLAY": bool,
    "DROP_HISTORY_CACHE": bool,
    "ENABLE_AUTOACCEPT": bool,
    "FARMING_MODE": int,
    "GAME_SEARCH_TIMEOUT": int,
    "GUI_SCALING": str,
    "ITEM_PICTURES_IN_TG": bool,
    "KILL_MONITOR_POSITION": bool,
    "LOBBY_CREATION_ATTEMPTS_BEFORE_SHUFFLE": int,
    "LOBBY_INVITE_MODE": int,
    "MAP_LOAD_DELAY": int,
    "NO_AVAST_MODE": bool,
    "PANEL_POSITION": str,
    "REPLACE_ERROR_ACCOUNT": bool,
    "ROUND_TARGET": int,
    "RUNNING2VS2": bool,
    "SEARCH_RETRIES_BEFORE_SHUFFLE": int,
    "SORT_ACCOUNTS_BY_LVL": bool,
    "START_FARM_WHEN_LAUNCHED": bool,
    "STATS_SKIN_PRICE": float,
    "STEAM_LAUNCH_OPTIONS": str,
    "STEAM_PATH": str,
    "TG_BOT_API_KEY": str,
    "TG_BOT_CHAT_ID": str,
    "TRADEOFFER_LINK": str,
}
SECRET_KEYS = frozenset({"TG_BOT_API_KEY", "TG_BOT_CHAT_ID", "TRADEOFFER_LINK"})
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
_SAVE_LOCK = threading.RLock()


def _is_sensitive(key: str) -> bool:
    upper = key.upper()
    return key in SECRET_KEYS or any(
        word in upper for word in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CREDENTIAL")
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _is_sensitive(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 40:
        raise ValidationError("settings_nested_too_deep")
    if type(value) is str and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValidationError("invalid_json_string")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item, depth + 1)
        return
    raise ValidationError("invalid_json_value")


def _validate(values: dict[str, Any]) -> None:
    if type(values) is not dict:
        raise ValidationError("settings_root_must_be_object")
    _validate_json(values)
    for key, value in values.items():
        expected = SETTINGS_TYPES.get(key)
        # isinstance(True, int) is true in Python; JSON booleans are NOT integers here.
        if expected is not None and type(value) is not expected:
            raise ValidationError(f"settings_type_mismatch:{key}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate_settings_key")
        result[key] = value
    return result


def _decode(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ValidationError("settings_file_too_large")
    try:
        values = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_object_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValidationError("invalid_json_number")),
        )
    except ValueError, UnicodeError, RecursionError:
        raise ValidationError("invalid_settings_json") from None
    if type(values) is not dict:
        raise ValidationError("settings_root_must_be_object")
    _validate(values)
    return values


def atomic_write(path: Path, content: bytes) -> None:
    """Same-directory replace keeps the old file intact if a write fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


class SettingsDocument:
    """Preserves unknown keys and missing known keys; repr/export redact secrets."""

    def __init__(self, values: dict[str, Any]) -> None:
        _validate(values)
        self._values = copy.deepcopy(values)

    def __repr__(self) -> str:
        return f"SettingsDocument(keys={len(self._values)})"

    def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._values.get(key, default))

    def set(self, key: str, value: Any) -> None:
        _validate({key: value})
        self._values[key] = copy.deepcopy(value)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        return copy.deepcopy(self._values) if include_secrets else _redact(self._values)

    def redacted(self) -> dict[str, Any]:
        return self.to_dict()

    def save(self, path: str | Path, *, backup: bool = True) -> None:
        SettingsStore.save(self, path, backup=backup)


class SettingsStore:
    @staticmethod
    def defaults() -> SettingsDocument:
        """New application defaults; every unattended legacy action starts disabled."""
        values = {key: expected() for key, expected in SETTINGS_TYPES.items()}
        values.update(
            {"ACCOUNT_LOGIN_ATTEMPTS": 1, "GUI_SCALING": "100%", "CS_RESOLUTION": "640x480"}
        )
        return SettingsDocument(values)

    @staticmethod
    def loads(content: str | bytes) -> SettingsDocument:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        return SettingsDocument(_decode(raw))

    @staticmethod
    def load(path: str | Path) -> SettingsDocument:
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_SETTINGS_BYTES + 1)
        except OSError:
            raise ValidationError("settings_read_failed") from None
        return SettingsDocument(_decode(raw))

    @staticmethod
    def save(document: SettingsDocument, path: str | Path, *, backup: bool = True) -> None:
        values = document.to_dict(include_secrets=True)
        _validate(values)
        try:
            raw = (json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
                "utf-8"
            )
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValidationError("settings_file_too_large")
            target = Path(path)
            with _SAVE_LOCK:
                if backup and target.exists():
                    # Backup preserves exact previous bytes, including a corrupt old file.
                    atomic_write(target.with_suffix(target.suffix + ".bak"), target.read_bytes())
                atomic_write(target, raw)
        except OSError:
            raise ValidationError("settings_save_failed") from None

    @staticmethod
    def restore_backup(path: str | Path) -> SettingsDocument:
        target = Path(path)
        backup = target.with_suffix(target.suffix + ".bak")
        document = SettingsStore.load(backup)
        SettingsStore.save(document, target, backup=False)
        return document
