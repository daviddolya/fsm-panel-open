# Independent QA review

Reviewed locally on Windows on 2026-10-13 by role A8. This review checks the new
application's contracts. It does not establish functional parity with FSM_PANEL.
The reviewer changed only independent tests and this report. Implementation fixes
belong to the coordinator or the Steam integration owner.

The initial core/licensing review used `ec32638`; fixes were verified against
`bf0456c`, `f79afc7`, and `750cf11`. The initial Steam worker review used `9c95140`;
its fixes were verified against `0f5bf2a`, followed by the GUI scope fix `e972afa`.
All ten reproduced findings below are resolved at this reviewed checkpoint.

## Reproduced defects

Each finding has a regression that failed before the corresponding implementation
change. Priorities describe consequences in an enabled implementation. Native game
automation and the live Steam adapter remain unavailable or restricted to stand use.

| ID | Priority | Failure and affected boundary | Regression | Verification |
| --- | --- | --- | --- | --- |
| QA-01 | P2 | Two SQLite connections could both commit against revision zero. The check preceded the write transaction. `src/fsm_panel/core/repository.py:217`, `AccountRepository.update_state`. | `test_stale_revision_cannot_win_through_a_second_database_connection` | Fixed in `bf0456c`; independently passed. |
| QA-02 | P1 | A database with matching column names but missing effect uniqueness was accepted as v0 or v1. This removed the durable duplicate-intent guarantee. `src/fsm_panel/core/migrations.py:68`, `admit_schema`. | `test_matching_column_names_cannot_bypass_effect_uniqueness`, both versions | Fixed in `bf0456c`; independently passed. |
| QA-03 | P2 | Revisions outside PostgreSQL INTEGER range passed API validation and produced HTTP 500. `src/fsm_license/schemas.py:17`, `EntitlementPut.revision`. | `test_large_revision_is_supported_or_rejected_before_database_failure`, `2**31` and `10**100` | Fixed in `bf0456c`; independently passed against PostgreSQL. |
| QA-04 | P1 | Cancelling ordinary stop left a live resource in STOPPING, which lease monitoring skipped. `src/fsm_panel/core/coordinator.py:258`, `Coordinator.stop`. | `test_cancelling_stop_cannot_leave_an_unmonitored_live_session`, one and three cancellations | Fixed in `bf0456c`; independently passed. |
| QA-05 | P1 | An adapter cancelling its own cleanup blocked the worker loop indefinitely. The first fix then swallowed caller cancellation during retry cleanup. `src/fsm_panel/core/coordinator.py:116`, `Coordinator._cleanup`. | `test_adapter_self_cancellation_cannot_spin_forever_during_launch_cleanup`; `test_caller_cancellation_during_retry_cleanup_is_not_an_adapter_self_cancel` | Fixed through `750cf11`; both independently passed. The initial infinite-loop reproduction ran in a bounded child Python process. |
| QA-06 | P1 | Resume admitted a persisted paused account before recovery. It became RUNNING without registration in the new coordinator's session monitor. `src/fsm_panel/core/coordinator.py:308`, `Coordinator.resume`; GUI startup command guard. | `test_persisted_paused_session_cannot_resume_before_recovery` | Fixed in `f79afc7`; independently passed. |
| QA-07 | P1 | The worker initialized a foreign database and accepted an operations table without its primary key. `worker/src/journal.ts:13`, `Journal.constructor`. | `test_worker_rejects_unrecognized_or_constraint_damaged_journal`, both cases | Fixed in `0f5bf2a`; independently passed through actual Node IPC. |
| QA-08 | P2 | Successful IPC payloads accepted a boolean asset amount and arbitrary diagnostic text. Dataclass annotations did not validate incoming values. `src/fsm_panel/trading/models.py:159` and `:190`, `InventoryResult.from_payload` and `TradeResult.from_payload`. | `test_inventory_success_payload_rejects_noninteger_asset_amount`; `test_trade_success_payload_rejects_arbitrary_diagnostic_code` | Fixed in `0f5bf2a`; independently passed. |
| QA-09 | P2 | An invalid trade destination reserved a core effect before worker validation. Rejection left UNKNOWN without a worker operation or offer ID available for reconciliation. `src/fsm_panel/trading/service.py:142`, `TradeService.send`. | `test_invalid_destination_is_rejected_before_reserving_external_effect` | Fixed in `0f5bf2a`; independently passed. |
| QA-10 | P2 | After sending for account A, reading B's inventory overwrote the account used for A's last-trade reconciliation. The GUI received `operation_conflict`. `src/fsm_panel/ui/trades.py:126`, `TradePanel._submit`. | `test_reconcile_last_trade_keeps_its_account_after_other_inventory_lookup` | Fixed in `e972afa`; independently passed through actual Qt and Node IPC in simulator mode. |

Source line numbers identify the final reviewed checkpoint, not the original faulty
version.

## Independent passing boundaries

The added tests also verify outcomes that did not require fixes:

- Lease verification rejects wrong signed issuer/audience and malformed headers,
  closing an earlier valid gate.
- Cancelling a dispatched HTTP renewal closes admission. A subsequent fresh online
  request can recover it.
- Reducing device slots denies the released device on renewal. Existing leases
  still expire within 60 seconds; revocation never prevents device release.
- Two independent SQLite connections race to begin one effect. Exactly one wins.
  A simulated crash followed by reopen preserves UNKNOWN and cannot resubmit.
- Failed cancellation cleanup blocks another launch until explicit recovery succeeds.
  Recovery cleans resources without starting a new launch.
- The actual Qt window can close repeatedly while admission is pending. It cancels
  the queued launch, persists STOPPED, and joins its worker without launching a game.
- Actual Node IPC refuses confirmation of a missing operation. A known UNKNOWN
  operation without an offer ID remains UNKNOWN during confirmation and reconciliation.
- Completion after lease loss retains the original account and offer scope. Another
  account cannot confirm the offer; repeating the owner's completion creates no new effect.

The approved completion policy permits confirmation of an already-created pending
offer after lease loss. This exemption requires a persisted operation and offer ID.
It does not permit creating, reconstructing, or sending a new offer. Read-only status
and reconciliation remain available without admission.

## Run the review suite

From an installed development checkout, using Python 3.14 and Node 24:

```powershell
$env:PYTHONPATH = 'src'
python -m pytest tests/qa -q
python -m ruff check tests/qa
python -m ruff format --check tests/qa
```

Set `FSM_TEST_POSTGRES_URL` to the dedicated test database before running the suite
to include PostgreSQL checks. The reused fixture creates a random schema per test
and removes only that schema. No production data or server-wide setting is changed.
An absent URL produces explicit skips and is not evidence of PostgreSQL validation.

For core branch coverage:

```powershell
python -m pytest tests/core tests/adapters tests/qa/test_persistence_contracts.py tests/qa/test_lifecycle_failures.py --cov=fsm_panel.core --cov-branch --cov-report=term-missing
```

The final core coverage run against `f79afc7` and `750cf11` passed 137 tests without
exclusions. It measured 217/240 core branches, or 90.42%, meeting the 90% target.
The final independent QA run passed all 27 cases in 4.10 seconds, with PostgreSQL
enabled and no skips. This includes the worker fixes and simulator trade-account
regression. Ruff lint and format checks passed for all seven QA Python files.

## Limits of this review

All credentials, accounts, database records, worker scenarios, and trade destinations
used for testing were synthetic. No real Steam login, inventory action, confirmation,
or trade was performed. The original installation and its private account data were
not used by this reviewer. The REA session remained with its assigned native owner.

Simulator and SDK-double tests cannot prove live Steam behavior. Real CS2 process
control, 50 real clients, a 24-hour soak, and validation on a clean Windows machine
remain stand gates. No claim of legacy parity or release readiness follows from this report.

The later GSI receiver and additional embedded Steam features are outside this
reviewed checkpoint. Their integration and regression validation require a subsequent
review. Parent-owned Windows process start/stop tests are separate evidence; they
do not replace live CS2 validation.
