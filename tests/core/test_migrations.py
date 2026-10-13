import sqlite3
from contextlib import closing

import pytest

from fsm_panel.core import AccountRepository, ValidationError


def test_future_database_version_is_refused_without_mutation(tmp_path):
    path = tmp_path / "future.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=99")
    original = path.read_bytes()
    with pytest.raises(ValidationError, match="database_schema_newer"):
        AccountRepository(path)
    assert path.read_bytes() == original


def test_known_unversioned_schema_gets_backup_before_version_upgrade(tmp_path):
    path = tmp_path / "old.sqlite"
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=0")
    with AccountRepository(path) as repo:
        assert repo.get(account.id).login == "synthetic"
    backup = path.with_suffix(".sqlite.v0.bak")
    assert backup.is_file()
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT login FROM accounts").fetchone()[0] == "synthetic"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_migration_backup_preserves_old_schema(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    with AccountRepository(path) as repo:
        repo.add_account("synthetic")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=0")

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr("fsm_panel.core.migrations.os.replace", fail)
    with pytest.raises(ValidationError, match="database_backup_failed"):
        AccountRepository(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert not list(tmp_path.glob(".migration-*"))


@pytest.mark.parametrize(
    "kind", ["corrupt", "unrecognized", "incomplete", "bad_columns", "missing_tables"]
)
def test_invalid_databases_are_never_silently_reset(tmp_path, kind):
    path = tmp_path / "invalid.sqlite"
    if kind == "corrupt":
        path.write_bytes(b"synthetic-not-a-database")
    else:
        with closing(sqlite3.connect(path)) as connection:
            if kind == "unrecognized":
                connection.execute("CREATE TABLE foreign_data (id TEXT)")
            elif kind == "incomplete":
                connection.execute("PRAGMA user_version=1")
            elif kind == "missing_tables":
                connection.execute("CREATE TABLE accounts (id TEXT)")
                connection.execute("PRAGMA user_version=1")
            else:
                connection.execute("CREATE TABLE accounts (id TEXT)")
    original = path.read_bytes()
    with pytest.raises(ValidationError):
        AccountRepository(path)
    assert path.read_bytes() == original
