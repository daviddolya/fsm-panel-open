"""SQLite schema admission and backed-up, transactional migration to version 1."""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .models import ValidationError

SCHEMA_VERSION = 1
_COLUMNS = {
    "accounts": {
        "id",
        "login",
        "login_key",
        "display_name",
        "state",
        "secret_ref",
        "error_code",
        "revision",
    },
    "events": {"sequence", "account_id", "kind", "state", "code", "created_at", "data"},
    "effects": {"id", "account_id", "kind", "idempotency_key", "state", "created_at", "updated_at"},
    "owned_processes": {"token", "account_id", "pid", "create_time", "executable"},
}


def admit_schema(connection: sqlite3.Connection) -> bool:
    """Return whether an existing recognized v0 database needs a backup.

    Never downgrade a future version or guess how to migrate an unknown schema.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValidationError("database_schema_newer_than_application")
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not existing:
        if version != 0:
            raise ValidationError("database_schema_incomplete")
        return False
    if version == SCHEMA_VERSION and not _COLUMNS.keys() <= existing:
        raise ValidationError("database_schema_incomplete")
    if version == 0 and ("accounts" not in existing or not existing <= _COLUMNS.keys()):
        raise ValidationError("unrecognized_database_schema")
    for table in existing & _COLUMNS.keys():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not _COLUMNS[table] <= columns:
            raise ValidationError("unrecognized_database_schema")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValidationError("database_integrity_failed")
    return version == 0


def backup_database(connection: sqlite3.Connection, path: str) -> None:
    """SQLite backup API includes committed WAL pages; same-directory atomic replace."""
    if path == ":memory:":
        return
    target = Path(path).with_suffix(Path(path).suffix + ".v0.bak")
    descriptor, name = tempfile.mkstemp(prefix=".migration-", suffix=".sqlite", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as backup:
            connection.backup(backup)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError, sqlite3.DatabaseError:
        raise ValidationError("database_backup_failed") from None
    finally:
        temporary.unlink(missing_ok=True)


def initialize_schema(connection: sqlite3.Connection, path: str, sql: str) -> None:
    try:
        if admit_schema(connection):
            backup_database(connection, path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
    except ValidationError:
        connection.close()
        raise
    except sqlite3.DatabaseError:
        connection.rollback()
        connection.close()
        raise ValidationError("database_initialization_failed") from None
