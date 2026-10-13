"""SQLite accounts and append-only state journal, updated in one transaction."""

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .migrations import initialize_schema
from .models import (
    Account,
    AccountState,
    DuplicateEffect,
    Effect,
    EffectState,
    Event,
    StateConflict,
    ValidationError,
)

_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_account(account: Account) -> None:
    if (
        not account.id
        or len(account.id) > 128
        or not account.login.strip()
        or account.login != account.login.strip()
        or len(account.login) > 128
        or any(ord(char) < 32 for char in account.login)
        or len(account.display_name) > 256
    ):
        raise ValidationError("invalid_account")


def _safe_code(code: str | None) -> None:
    if code is not None and not _CODE.fullmatch(code):
        raise ValidationError("invalid_event_code")


class AccountRepository:
    """Thread-safe metadata repository. Passwords are stored only by SecretStore.

    One repository may be shared by a Qt reader and an asyncio worker. A coordinator
    itself belongs to one asyncio loop. Close the repository after the worker stops.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        initialize_schema(
            self._connection,
            self.path,
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                login TEXT NOT NULL,
                login_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                state TEXT NOT NULL,
                secret_ref TEXT,
                error_code TEXT,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                code TEXT,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS events_by_account ON events(account_id, sequence);
            CREATE TABLE IF NOT EXISTS effects (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owned_processes (
                token TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                pid INTEGER NOT NULL,
                create_time REAL NOT NULL,
                executable TEXT NOT NULL
            );
            PRAGMA user_version=1;
            """,
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connection:
            yield self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> AccountRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            login=row["login"],
            display_name=row["display_name"],
            state=AccountState(row["state"]),
            secret_ref=row["secret_ref"],
            error_code=row["error_code"],
            revision=row["revision"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(
            sequence=row["sequence"],
            account_id=row["account_id"],
            kind=row["kind"],
            state=AccountState(row["state"]),
            code=row["code"],
            created_at=row["created_at"],
            data=tuple(json.loads(row["data"]).items()),
        )

    @staticmethod
    def _effect(row: sqlite3.Row) -> Effect:
        return Effect(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            idempotency_key=row["idempotency_key"],
            state=EffectState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def add_account(
        self,
        login: str,
        display_name: str = "",
        *,
        secret_ref: str | None = None,
        account_id: str | None = None,
    ) -> Account:
        account = Account(
            account_id or str(uuid.uuid4()), login, display_name, secret_ref=secret_ref
        )
        self.add_accounts([account])
        return account

    def add_accounts(self, accounts: Iterable[Account]) -> list[Account]:
        accounts = list(accounts)
        for account in accounts:
            _validate_account(account)
            if account.state != AccountState.IDLE or account.revision != 0:
                raise ValidationError("new_account_must_be_idle")
        try:
            with self._transaction() as db:
                for account in accounts:
                    db.execute(
                        "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)",
                        (
                            account.id,
                            account.login,
                            account.login.casefold(),
                            account.display_name,
                            account.state.value,
                            account.secret_ref,
                            None,
                            0,
                        ),
                    )
                    db.execute(
                        "INSERT INTO events(account_id,kind,state,created_at) VALUES (?,?,?,?)",
                        (account.id, "account_added", account.state.value, utc_now()),
                    )
        except sqlite3.IntegrityError:
            raise ValidationError("duplicate_account") from None
        return accounts

    def get(self, account_id: str) -> Account:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError("account_not_found")
        return self._account(row)

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM accounts ORDER BY rowid").fetchall()
        return [self._account(row) for row in rows]

    def update_state(
        self,
        account_id: str,
        state: AccountState,
        *,
        code: str | None = None,
        expected_revision: int | None = None,
    ) -> Event:
        """Atomically update the snapshot and append its event; optional optimistic check."""
        _safe_code(code)
        state = AccountState(state)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise KeyError("account_not_found")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StateConflict("stale_account_revision")
            db.execute(
                "UPDATE accounts SET state=?,error_code=?,revision=revision+1 WHERE id=?",
                (state.value, code, account_id),
            )
            cursor = db.execute(
                "INSERT INTO events(account_id,kind,state,code,created_at) VALUES (?,?,?,?,?)",
                (account_id, "state_changed", state.value, code, utc_now()),
            )
            event_row = db.execute(
                "SELECT * FROM events WHERE sequence=?", (cursor.lastrowid,)
            ).fetchone()
        return self._event(event_row)

    def record_retry(self, account_id: str, attempt: int) -> Event:
        if type(attempt) is not int or not 1 <= attempt <= 10:
            raise ValidationError("invalid_attempt")
        with self._transaction() as db:
            account = self.get(account_id)
            cursor = db.execute(
                "INSERT INTO events(account_id,kind,state,code,created_at,data) VALUES (?,?,?,?,?,?)",
                (
                    account_id,
                    "launch_retry",
                    account.state.value,
                    "transient_launch_failure",
                    utc_now(),
                    json.dumps({"attempt": attempt}),
                ),
            )
            row = db.execute(
                "SELECT * FROM events WHERE sequence=?", (cursor.lastrowid,)
            ).fetchone()
        return self._event(row)

    def events(
        self, *, after: int = 0, account_id: str | None = None, limit: int = 1000
    ) -> list[Event]:
        if not 1 <= limit <= 10000:
            raise ValidationError("invalid_event_limit")
        with self._lock:
            if account_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                    (after, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM events WHERE sequence>? AND account_id=? "
                    "ORDER BY sequence LIMIT ?",
                    (after, account_id, limit),
                ).fetchall()
        return [self._event(row) for row in rows]

    def reserve_effect(self, account_id: str, kind: str, idempotency_key: str) -> Effect:
        _safe_code(kind)
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValidationError("invalid_idempotency_key")
        self.get(account_id)
        effect_id, timestamp = str(uuid.uuid4()), utc_now()
        try:
            with self._transaction() as db:
                db.execute(
                    "INSERT INTO effects VALUES (?,?,?,?,?,?,?)",
                    (
                        effect_id,
                        account_id,
                        kind,
                        idempotency_key,
                        EffectState.PREPARED,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError:
            raise DuplicateEffect("effect_already_reserved") from None
        return self.get_effect(effect_id)

    def get_effect(self, effect_id: str) -> Effect:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM effects WHERE id=?", (effect_id,)
            ).fetchone()
        if row is None:
            raise KeyError("effect_not_found")
        return self._effect(row)

    def list_effects(self) -> list[Effect]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM effects ORDER BY rowid").fetchall()
        return [self._effect(row) for row in rows]

    def transition_effect(
        self, effect_id: str, expected: EffectState, state: EffectState
    ) -> Effect:
        allowed = {
            EffectState.PREPARED: {EffectState.IN_FLIGHT, EffectState.UNKNOWN},
            EffectState.IN_FLIGHT: {
                EffectState.SUCCEEDED,
                EffectState.FAILED_CONFIRMED,
                EffectState.UNKNOWN,
            },
            EffectState.UNKNOWN: {EffectState.SUCCEEDED, EffectState.FAILED_CONFIRMED},
        }
        if state not in allowed.get(expected, set()):
            raise StateConflict("invalid_effect_transition")
        with self._transaction() as db:
            result = db.execute(
                "UPDATE effects SET state=?,updated_at=? WHERE id=? AND state=?",
                (state.value, utc_now(), effect_id, expected.value),
            )
            if result.rowcount != 1:
                raise StateConflict("effect_state_changed")
        return self.get_effect(effect_id)

    def recover_effects(self) -> int:
        """Ambiguous effects become UNKNOWN; recovery NEVER repeats an external call."""
        with self._transaction() as db:
            result = db.execute(
                "UPDATE effects SET state=?,updated_at=? WHERE state IN (?,?)",
                (
                    EffectState.UNKNOWN.value,
                    utc_now(),
                    EffectState.PREPARED.value,
                    EffectState.IN_FLIGHT.value,
                ),
            )
        return result.rowcount

    def remember_process(
        self, token: str, account_id: str, pid: int, create_time: float, executable: str
    ) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO owned_processes VALUES (?,?,?,?,?)",
                (token, account_id, pid, create_time, executable),
            )

    def owned_processes(self, account_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            if account_id is None:
                rows = self._connection.execute("SELECT * FROM owned_processes").fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM owned_processes WHERE account_id=?", (account_id,)
                ).fetchall()
        return [dict(row) for row in rows]

    def forget_process(self, token: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM owned_processes WHERE token=?", (token,))
