# Steam worker

The worker supports inventory collection and durable trade creation, confirmation, and reconciliation. The default transport is a simulator. Live Steam behavior has not been tested on the assigned stand; no real account was authenticated during implementation.

## Run and verify

Use Node 24.19.x and pnpm 11.25.0. From the repository root:

```powershell
pnpm --dir worker install --frozen-lockfile
pnpm --dir worker build
pnpm --dir worker test
uv run pytest tests/trading tests/qa/test_worker_boundaries.py
```

Development runs `node worker/src/main.ts --state <absolute-worker.sqlite>`. A release runs `node worker/dist/main.js --state <absolute-worker.sqlite>`. Both default to `--mode simulation`. Default simulated inventory is empty; `--scenario <synthetic.json>` supplies test behavior. The scenario file never triggers network access.

The production adapter additionally requires `--mode live --allow-live-stand`. This explicit opt-in does not represent stand approval or release clearance. Keep production GUI actions disabled until their stand and dependency gates pass.

## Python integration

`NodeWorkerClient(command)` owns a subprocess and is an async context manager. Pass executable, script and state/scenario paths as separate command elements. Never use a shell, credential environment variables or credential arguments.

`TradeService(repository, worker, admission=None)` provides:

| Method | Input | Result |
| --- | --- | --- |
| `inventory` | account ID, `SteamCredentials`, inventory specs | `InventoryResult` |
| `send` | account ID, operation ID, trade URL, credentials, specs | `TradeResult` |
| `confirm` | account ID, operation ID, credentials | `TradeResult` |
| `reconcile` | account ID, operation ID, credentials | `TradeResult` |
| `status` | account ID, operation ID | Persisted `TradeResult` |

`SteamCredentials(account_name, password, shared_secret, identity_secret=None)` hides all fields from `repr`. `InventorySpec(app_id=730, context_id="2")` identifies an inventory. Credentials travel only through private stdin and remain absent from both journals. Keep them in the application's existing secret store outside a request.

Admission is an async function accepting an account ID and returning the verified online lease deadline in Unix milliseconds. Absence of admission blocks inventory and send. The facade rechecks it every 250 ms while new work runs. The worker checks its deadline again immediately before dispatch. Confirmation, reconciliation, status and cancellation remain available after licence expiry.

An admission provider can call `client.gate.require_valid()` and return `claims.expires_at * 1000`. Its deadline must be in the next 60 seconds. Stand simulations use a test provider and never imply licence-service or Steam parity.

## IPC and error boundary

Contracts are `contracts/worker-request.json` and `contracts/worker-response.json`. Protocol version is 1. Every request has a unique `request_id`, a method, a 10–300,000 ms deadline and typed params. `cancel` targets the original request ID. Input frames are limited to 64 KiB. Stdout contains only JSON Lines; SDK diagnostics become fixed codes on stderr. Python drains and discards raw stderr.

Only one operation runs per worker; cancellation remains responsive. Duplicate request IDs are rejected. The process exits on stdin closure after cancellation and journal settlement. Python terminates a hung owned worker after bounded cleanup. Request errors contain fixed codes, not SDK messages, cookies, URLs or tokens.

## State and recovery

| State | Meaning | Permitted next step |
| --- | --- | --- |
| `not_sent` | The send boundary was not crossed | Inspect; a new explicit intent may be created |
| `created_pending_confirmation` | Steam returned an offer ID requiring confirmation | Confirm or reconcile |
| `confirmed` | Mobile confirmation callback succeeded | Inspect/reconcile; this does not mean recipient acceptance |
| `sent` | Steam reported an active, accepted or escrowed outgoing offer | Inspect/reconcile |
| `failed` | Preparation failed, or a known offer was authoritatively terminal | Inspect; a new explicit intent may be created |
| `unknown_outcome` | Creation/confirmation may have happened | Reconcile; never resend automatically |

The worker commits `unknown_outcome` with a dispatch phase before calling send or confirm. A lost callback, timeout, disconnect or cancellation cannot reopen that boundary. Reusing an operation ID only retrieves its existing state. A new operation ID is also blocked for an account while another offer is pending or unknown. The SQLite unique index enforces this across processes.

The worker journal admits only its recognized schema and validates primary keys, its partial unique index and stored values. A transactionally acquired PID/nonce owner prevents a second process from running recovery while the first owner lives. A dead PID can be recovered automatically; a live reused PID is conservatively blocked. Do not delete journal rows to force a retry.

The core `ExternalEffectsLedger` separately reserves the caller's intent before IPC. `SUCCEEDED` there means offer creation was acknowledged, including pending confirmation. It does not mean recipient acceptance. Core recovery marks interrupted effects unknown. Reconciliation can settle those effects only from a known offer result.

Without an offer ID, the worker cannot establish whether Steam received the request. Reconciliation preserves unknown and does not scan recent offers or treat an empty result as proof of absence. Account-level sending stays blocked until authoritative evidence becomes available. Retain both SQLite databases and their WAL files during backup/recovery.

## Inventory and confirmed source behavior

The external `looter_core.js` uses argv credentials, default `730/2`, SteamUser/TOTP, web-session cookies, concurrent inventories, partial failure skipping, one offer, and mobile confirmation. It exits 1 after confirmation, exits -1 on errors, has no explicit `sent` shutdown, and can repeat work on another `webSession`.

The rebuild deliberately fixes those defects. It deduplicates inventory and asset identities, rejects inconsistent duplicate amounts, excludes incomplete inventories, refuses empty offers and exposes partial failure. `allow_partial=True` retains the legacy skip behavior. `False` requires every requested inventory to succeed. Confirmation is a separate durable command rather than an implicit process exit.

The SDK adapter uses `getInventoryContents`; the pinned `steamcommunity` implementation follows `more_items/last_assetid` internally. The transport contract also supports page-by-page synthetic inputs for pagination, duplicate-page and partial-page tests. A synthetic pagination test is not a live pagination test.

Source SHA-256, exact shipped/locked versions, static line evidence and verified npm-file comparisons are recorded in `worker/audit/legacy-provenance.json`. Regenerate it with `worker/scripts/audit_legacy.py <original-looter-directory> <output.json>`. The script reads source and tarballs without importing or executing the original.

The installed original differs from its lock for community, tradeoffer-manager and steam-session. Its manager removes both the Web API key callback error check and key assignment at upstream lines 199–204; the clean replacement explicitly uses supported access-token cookies. Shipped community changes `components/users.js:529` inventory page count from 2000 to 1000. Neither modification is vendored; clean community 3.50.3 independently uses 1000.

Maintainer references: [manager API](https://github.com/DoctorMcKay/node-steam-tradeoffer-manager/wiki/TradeOfferManager), [offer API](https://github.com/DoctorMcKay/node-steam-tradeoffer-manager/wiki/TradeOffer), [steam-user API](https://github.com/DoctorMcKay/node-steam-user), [pnpm build settings](https://pnpm.io/settings).

## Dependencies and remaining gates

Direct runtime packages are clean pinned npm releases: steam-user 5.3.0, steamcommunity 3.50.3, steam-tradeoffer-manager 2.13.0 and steam-totp 2.1.2. All four declare MIT. The pnpm lock records exact transitive versions and integrity hashes. No shipped node_modules files are copied.

`pnpm-workspace.yaml` explicitly denies the reviewed protobufjs postinstall hooks. Version 6 creates an unused CLI dependency directory and prints a semver warning; version 7 only prints that warning. Neither generates runtime code. Other unreviewed hooks stay blocked.

The initial `pnpm audit --prod` reported 29 advisories: 2 critical, 14 high and 13 moderate, including request, cheerio dependencies, protobufjs and adm-zip. These are release blockers requiring separate dependency remediation and compatibility validation. An SDK import test proves only that these modules load locally.

Stand acceptance must cover real cookies/authentication, app entitlement, inventory pagination/rate limits, pending/sent/escrow states, mobile confirmation, disconnects, response loss, cancellation and restart. Real credentials, database files and captures stay outside Git. Telegram delivery is unavailable until its specific source contract is recovered.
