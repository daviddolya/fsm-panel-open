# Core and adapter integration

The core provides a working account lifecycle simulator and persistence infrastructure.
It does not implement unverified native Panel, Steam, or CS2 behavior.
Simulator tests establish the new contracts; they do not establish legacy parity.

## Run a synthetic lifecycle

This example creates no operating-system process and contacts no external service.

```python
import asyncio

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator, import_accounts


async def main():
    with AccountRepository() as repository:
        imported = import_accounts(
            "synthetic_example:synthetic_password",
            repository,
            MemorySecretStore(),
        )
        coordinator = Coordinator(
            repository,
            game=FakeGame(),
            lease=FakeLease(),
            max_concurrency=5,
        )
        coordinator.subscribe(lambda event: print(event.kind, event.state.value))
        account_id = imported.accounts[0].id
        await coordinator.start(account_id)
        await coordinator.pause(account_id)
        await coordinator.resume(account_id)
        await coordinator.stop(account_id)
        await coordinator.close()


asyncio.run(main())
```

The observed states are `starting`, `running`, `paused`, `running`, `stopping`, and `stopped`.
Call `close()` before closing the repository or its event loop.

## Public contracts

| API | Contract |
| --- | --- |
| `AccountRepository(path)` | SQLite metadata, state, events, effects, and process ownership. Use `:memory:` for isolated tests. |
| `add_account(login, display_name="")` | Add metadata only; returns an immutable `Account` with an opaque UUID. |
| `get(id)`, `list_accounts()` | Read immutable account snapshots. Passwords are never included. |
| `events(after=0, account_id=None, limit=1000)` | Read an incremental, ascending journal. State and its event commit together. |
| `Coordinator(repository, game=..., lease=...)` | Serialize operations per account on one asyncio loop. |
| `start(id)`, `start_batch(ids)` | Complete when admitted launches reach running or a terminal outcome. Batch maximum: 50 distinct IDs. |
| `pause(id)`, `resume(id)` | Operate on running or paused sessions; other states return unchanged. Resume requires a valid lease. |
| `stop(id)`, `cancel(id)` | Interrupt an in-progress launch, clean owned resources, and persist its final state. No lease required. |
| `recover()` | Clean interrupted sessions and mark ambiguous external effects unknown; never restart work. |
| `check_leases()` | Check admitted sessions now. The automatic monitor also checks at one-second intervals by default. |
| `subscribe(callback)` | Return an unsubscribe function. Callbacks receive immutable `Event` objects on the asyncio thread. |

`Account` has `id`, `login`, `display_name`, `state`, `secret_ref`, `error_code`, and `revision`.
Its representation hides login, display name, and secret reference.
An `Event` has `sequence`, `account_id`, `kind`, `state`, `code`, `created_at`, and immutable `data` pairs.
Events include stable error codes, never raw adapter exceptions or credentials.

Repository reads and writes use a lock around a cross-thread SQLite connection.
The GUI may call `list_accounts()` while the coordinator updates states.
GUI integration must forward subscriber events through a Qt signal; never mutate widgets in the callback.

`max_concurrency` bounds concurrent launch operations and accepts integers from 1 through 50.
The coordinator admits at most 50 account sessions across overlapping batches.
Successful sessions remain running until stopped; this simulator does not invent farming progression.

## Admission, failures, and cancellation

`LeaseGate.admit(account_id)` is asynchronous and returns `Admission(allowed: bool, reason: str)`.
Only an actual `True` grants admission. Exceptions and malformed responses deny it.
The coordinator checks before every launch attempt, after launch, and before resume.
A background monitor stops sessions when admission is revoked or expires.
`stop`, `cancel`, `recover`, and repository exports do not require admission.

The default adapters are `DenyLease` and `NativeGameUnavailable`.
An explicitly granted lease still cannot make the native adapter launch a game.
Its launch returns `unavailable` with `native_behavior_unverified`.

`RetryPolicy` defaults to three attempts, a 0.1-second delay, a 30-second launch deadline,
and a 10-second cleanup deadline. Only `TransientError` permits a retry after successful cleanup.
Timeouts and unknown failures are not retried. Cleanup failure records `cleanup_required`
and blocks further starts until explicit stop or recovery succeeds.

Cancellation during launch or backoff cleans resources and propagates `CancelledError` to the launch caller.
The stop caller receives the persisted account snapshot.
Repeated cancellation cannot interrupt the bounded launch-cleanup operation.
Operations use monotonic intervals. `FakeClock.advance()` drives deterministic simulated delays;
adapter safety deadlines use the asyncio loop's real monotonic clock.

## Secret-safe imports

`import_accounts(text, repository, secret_store, format="lines")` returns
`ImportResult(accounts=tuple[Account, ...], count=int)`.

Supported reconstruction formats are:

- `login:password`, one account per nonempty line. Passwords may contain colons.
- CSV with exact `login,password` headers and optional `display_name`; standard CSV quoting applies.

These are new explicit input contracts, not recovered legacy maFile/session formats.
The complete input is validated before writing. Logins are unique after Unicode case folding.
Credentials go to `SecretStore`; the metadata transaction stores only opaque references.
Failure rolls back newly written secret references and reports a stable error code.
Callers must clear their input widgets. Python strings cannot be reliably wiped from memory.

`DPAPISecretStore(directory)` protects data with Windows current-user DPAPI and forbids UI prompts.
No plaintext fallback exists. An injected `SecretProtector` permits portable tests.
`MemorySecretStore` is explicitly limited to tests and simulator sessions.
DPAPI protects stored data from other accounts; same-user malware can still call DPAPI.

## Settings compatibility and private storage

The supplied `FSM_PANEL v.3.1.2/settings/settings.json` established 49 key names and exact JSON types.
No original values are checked into this project. Names do not establish native feature semantics.
`SettingsStore.defaults()` supplies new application defaults with unattended legacy actions disabled.
It does not reproduce the original defaults.

`SettingsStore.load(path)` and `SettingsStore.loads(text)` import legacy JSON.
`SettingsDocument.get()` returns a copy; `set()` validates without coercion.
Boolean, integer, and floating-point types remain distinct.
Unknown fields, nested JSON values, missing known keys, and Unicode values are preserved.
Duplicate keys, nonfinite numbers, excessive nesting, invalid types, and corrupt JSON are rejected.

`document.to_dict()` and `document.redacted()` hide known secret fields and recursively named
password, secret, token, API key, or credential fields. `get()` and `to_dict(include_secrets=True)`
are explicit private-data APIs. Names cannot detect secrets hidden under arbitrary unrelated keys.

`SettingsStore.save(document, path)` is an explicit **plaintext legacy export**.
It preserves compatibility values and therefore must not be used as the app's private settings store.
It atomically replaces the target and keeps the exact previous bytes in `path + ".bak"`.
`restore_backup(path)` validates the backup before replacing the current file.

Use the vault for application settings:

```python
from fsm_panel.adapters.secrets import DPAPISecretStore
from fsm_panel.adapters.settings_vault import SettingsVault
from fsm_panel.core import SettingsStore

secrets = DPAPISecretStore("private/secrets")
vault = SettingsVault(secrets)
document = SettingsStore.defaults()
vault.save(document, "private/settings.vault.json")
document = vault.load("private/settings.vault.json")
```

The versioned vault envelope contains ordinary values and opaque secret references.
Sensitive fields are encrypted as JSON values, including nested objects and arrays.
New saves use `fsm-panel-settings-v2`, which also protects `PROXY_FOR_TG` and case
variants of secret keys. Reading v1 is read-only and retains its historical nested
secret checks. The next save seals the current document and the previous document's
backup as v2. Previously retained external copies are unchanged. These format labels
provide compatibility, not authenticated downgrade protection.
`save(document, path, backup=True)`, `load(path)`, and `restore_backup(path)` mirror the import APIs.
Save legacy imports to a distinct vault destination; the vault refuses to back up a plaintext legacy file.
Fresh references on every save keep earlier backups decryptable. No automatic reference deletion or GC runs,
including after uncertain file-write outcomes. Removing orphaned ciphertext requires a future explicit GC tool.
Keep the vault, its backups, and its secret-store directory together for recovery under the same Windows user.

## SQLite recovery and migrations

On startup, call `await coordinator.recover()` before accepting GUI work.
Interrupted accounts become `interrupted` after successful cleanup or `failed/cleanup_required` otherwise.
Persisted process records must be removed by the owning adapter before cleanup is considered successful.
An unavailable adapter cannot silently claim a recorded process was cleaned.

SQLite uses foreign keys, WAL journaling, and full synchronous writes.
Schema version 1 is transactional. A recognized unversioned prototype schema gets a SQLite backup
at `database_path + ".v0.bak"` before migration. This backup includes committed WAL content.
Unknown schemas, future versions, and damaged databases fail closed; they are never silently reset.
Before restoring a database backup, stop the application and preserve the current database and sidecars.
Restore a known-compatible backup together with the corresponding secret-store files.

## Owned processes and windows

`OwnedProcessController` is independent infrastructure; it is not wired to real Steam or CS2 commands.
`launch(account_id, argv, cwd=None)` requires an absolute executable path and an argument sequence.
It uses `shell=False`, hidden Windows startup, closed inherited handles, and private standard streams.
Credentials must never be put in argv by a future adapter.

Ownership records persist a random token, account ID, PID, creation time, and executable path.
Before signaling a process, cleanup rechecks its creation time and executable.
A reused PID is forgotten without signaling the unrelated process.
Descendants are registered only while their parent's ownership is verified.
Cleanup tries terminate, then kill after a bounded wait; access-denied records remain for recovery.
Cancellation during a spawn waits for the spawning thread to finish, then cleans the new owned process.

`WindowsWindowLookup.for_pid(pid)` enumerates matching windows without interacting with them.
It does not infer account identity from a window title.
`FakeProcessBackend` and `FakeWindowLookup` exercise these contracts without real processes or games.

Process discovery and cleanup are best-effort snapshots. Descendants spawned after enumeration,
or inherited after their parent disappears, require a future Windows Job Object adapter for a stronger guarantee.
No sandbox, BES, focus, lobby, matchmaking, anti-AFK, autoaccept, or game-config behavior is claimed here.

## External effects

`ExternalEffectsLedger.reserve(account_id, kind, idempotency_key)` commits intent before an external call.
`begin(effect_id)` atomically changes prepared to in-flight; only one caller can claim it.
Outcomes are `succeeded`, `failed_confirmed`, or `unknown`.
Recovery marks both prepared and in-flight records unknown and never repeats the external call.

`reconcile(effect_id, succeeded=...)` requires an authoritative external status check.
It records that check; it does not submit anything. The same idempotency key remains reserved forever,
including after a confirmed failure. A new explicit user intent must use a new key.
This prevents automatic duplicate submission; it is not a claim of distributed exactly-once delivery.

## Verification

Run from the repository root after installing the project's development dependencies:

```powershell
python -m pytest tests/core tests/adapters -q
python -m pytest tests/core tests/adapters --cov=fsm_panel.core --cov-branch --cov-report=term-missing
python -m mypy src/fsm_panel/core src/fsm_panel/adapters
python -m ruff check src/fsm_panel/core src/fsm_panel/adapters tests/core tests/adapters
python -m ruff format --check src/fsm_panel/core src/fsm_panel/adapters tests/core tests/adapters
```

Tests use synthetic accounts and generated command/JSON sequences. They exercise 50 simulated accounts,
bounded concurrency, failure recovery, unknown effects, cancellation, schema admission, settings backups,
vault failures, PID reuse, child cleanup, and a current-user DPAPI roundtrip on Windows.
The DPAPI test is skipped on other platforms, where a fake protector tests the storage contract.
Real 50-client load, 24-hour stability, clean-machine release validation, and legacy parity remain stand gates.

Local result on 2026-10-13: 127 core/adapter tests passed on Windows with Python 3.14.6.
Ruff lint, Ruff format check, and mypy passed. The preceding 126-test coverage run measured
195 of 210 core branches covered (92.86%); the final added test covers process-stop cancellation.
The original 49-key settings document also passed a read-only, in-memory roundtrip with exact types.
Its values were neither displayed nor copied into committed fixtures.
