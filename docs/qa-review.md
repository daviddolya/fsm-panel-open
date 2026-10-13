# Independent QA review

Reviewed locally on Windows on 2026-10-13 by role A8. This review checks the new
application's contracts. It does not establish functional parity with FSM_PANEL.
The reviewer changed only independent tests and this report. Implementation fixes
belong to the coordinator or the Steam integration owner.

The initial core/licensing review used `ec32638`; fixes were verified against
`bf0456c`, `f79afc7`, and `750cf11`. The initial Steam worker review used `9c95140`;
its fixes were verified against `0f5bf2a`, followed by the GUI scope fix `e972afa`.
The follow-up review includes GSI `58b6b5d`, the embedded Steam extension `8dd666d`,
packaging/smoke `e0d0e77`, and overlapping lease sweeps `286917c`. All fifteen
reproduced findings below are resolved at these reviewed checkpoints.

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

The source line numbers above identify the baseline checkpoint, not the original
faulty version.

## Extension review

| ID | Priority | Failure and affected boundary | Regression | Verification |
| --- | --- | --- | --- | --- |
| QA-11 | P2 | Cancelling GSI shutdown discarded ownership before closing active requests. An old incomplete HTTP request could still update telemetry after a second `close()` returned. `src/fsm_panel/gsi/http.py:160`, `GsiServer._close`. | `test_cancelled_close_cannot_orphan_a_connection_that_still_updates_telemetry` | Fixed in `58b6b5d`; independently passed over actual localhost HTTP. |
| QA-12 | P2 | After activity cancellation and restart, terminal worker status left the matching core effect UNKNOWN, permanently blocking a fresh activity. `src/fsm_panel/trading/service.py:458`, `TradeService.activity_status`. | `test_authoritative_cancelled_activity_can_release_core_unknown_after_restart` | Fixed in `8dd666d`; independently passed through actual Node IPC and disk reopen, including status without a lease. |
| QA-13 | P1 | Reusing a batch operation ID with a different asset group after restart dispatched another offer. Only child IDs were reserved; the complete batch identity was not durable. `src/fsm_panel/trading/service.py:273`, `TradeService.send_grouped`. | `test_batch_id_cannot_be_reused_to_dispatch_a_different_group_after_restart` | Fixed in `8dd666d`; independently passed through actual Node IPC and disk reopen. |
| QA-14 | P2 | Desktop smoke recorded success before asynchronous cleanup; a real worker cleanup exception still produced a successful report and exit decision. `src/fsm_panel/ui/smoke.py:25`, `DesktopSmoke.finish`. | `test_smoke_cannot_report_success_when_owned_worker_shutdown_fails` | Fixed in `e0d0e77`; independently passed with a controlled cleanup failure after the actual Qt/Node roundtrip. |
| QA-15 | P2 | A second lease sweep returned while the first was still cleaning a revoked live session in STOPPING. `src/fsm_panel/core/coordinator.py:357`, `Coordinator.check_leases`. | `test_concurrent_lease_sweep_waits_for_an_existing_revocation_cleanup` | Found by the coordinator's load run, reproduced independently with an event-controlled stop, and fixed in `286917c`; independently passed. |

The follow-up also verifies that UNKNOWN activity status survives restart and cannot
authorize another dispatch. Authoritative terminal activity status may settle only
the matching account/operation effect. Existing-offer completion policy is unchanged.

Packaging source review checked the frozen worker path against the staging layout,
the bundled Node runtime and production dependency installation, the research label,
file hashes, and smoke cleanup validation. The initial worker path mismatch was
already scheduled for replacement and is corrected in `e0d0e77`. This source review
does not establish that a final packaged executable has passed on a clean machine.

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

The baseline independent QA run passed all 27 cases in 4.10 seconds, with PostgreSQL
enabled and no skips. The final extension run used:

```powershell
python -m pytest tests/qa tests/gsi -m 'not postgres' -q
```

It passed 80 cases in 12.40 seconds: 31 QA cases and 49 GSI cases. The two PostgreSQL
QA cases were deliberately deselected to avoid running beside the coordinator's
database suite; their previous passing result remains baseline evidence. The QA suite
now contains 33 cases. Ruff lint and format checks passed for all ten QA Python files.
The three embedded QA cases were rerun successfully with resource and unraisable
exception warnings treated as errors after explicitly closing their SQLite polling
connections. This was test-helper cleanup, not a production database defect.

The final independent core coverage run after `286917c` passed 140 tests in 4.49 seconds.
It measured 217/240 core branches, or 90.42%, meeting the 90% target. No custom coverage
exclusions were introduced; the tool's default exclusions include protocol stubs.
Coordinator-owned full-suite and packaging results are recorded separately in
`docs/VERIFICATION.md` and must retain their own checkpoint and execution scope.

## Limits of this review

All credentials, accounts, database records, worker scenarios, and trade destinations
used for testing were synthetic. No real Steam login, inventory action, confirmation,
or trade was performed. The original installation and its private account data were
not used by this reviewer. The REA session remained with its assigned native owner.

Simulator and SDK-double tests cannot prove live Steam behavior. Real CS2 process
control, 50 real clients, a 24-hour soak, and validation on a clean Windows machine
remain stand gates. No claim of legacy parity or release readiness follows from this report.

The follow-up covers the standalone GSI receiver and embedded Steam source/API
extensions at the named checkpoints. It does not claim that these features are wired
to production GUI game actions. The Windows process tests use disposable Python
children; they do not replace live CS2 validation. Frozen build execution and release
gates retain the separately documented validation scope.
