import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from fsm_panel.core import AccountRepository, AccountState, StateConflict, ValidationError


def test_stale_revision_cannot_win_through_a_second_database_connection(tmp_path):
    path = tmp_path / "state.sqlite"
    with AccountRepository(path) as first, AccountRepository(path) as second:
        account = first.add_account("synthetic_revision_race")
        barrier = threading.Barrier(2)

        def synchronize_write(statement):
            if statement.lstrip().upper().startswith("UPDATE ACCOUNTS"):
                # Force competing writes after any optimistic reads. An implementation
                # that acquires a write lock before reading also passes this contract.
                try:
                    barrier.wait(timeout=0.25)
                except threading.BrokenBarrierError:
                    pass

        first._connection.set_trace_callback(synchronize_write)
        second._connection.set_trace_callback(synchronize_write)

        def update(repo, state):
            try:
                repo.update_state(account.id, state, expected_revision=0)
                return "committed"
            except StateConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(update, first, AccountState.RUNNING),
                workers.submit(update, second, AccountState.FAILED),
            ]
            outcomes = [future.result(timeout=5) for future in futures]

        assert sorted(outcomes) == ["committed", "conflict"]
        assert first.get(account.id).revision == 1
        assert len(first.events(account_id=account.id)) == 2


@pytest.mark.parametrize("schema_version", [0, 1])
def test_matching_column_names_cannot_bypass_effect_uniqueness(tmp_path, schema_version):
    path = tmp_path / "missing-constraint.sqlite"
    with AccountRepository(path) as repository:
        repository.add_account("synthetic_migration")
    with closing(sqlite3.connect(path)) as connection:
        # A damaged/foreign schema with identical names but no uniqueness guarantee
        # must not be admitted as a recognized migration or usable current schema.
        connection.executescript(
            "ALTER TABLE effects RENAME TO previous_effects;"
            "CREATE TABLE effects AS SELECT * FROM previous_effects;"
            "DROP TABLE previous_effects;"
            f"PRAGMA user_version={schema_version};"
        )
    original = path.read_bytes()
    with pytest.raises(ValidationError):
        with AccountRepository(path):
            pass
    assert path.read_bytes() == original
