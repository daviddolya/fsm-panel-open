# Independent offline reconstruction QA

Date: 2026-10-13. Review started at `791cd8a`; fixes were independently checked
through `d262e64` in a separate QA worktree. The reviewer owns the tests and this
report, not the implementation.

The video, BES and launch checks found five issues: lost recovery metadata, a
delayed admission race, an unguarded original-distribution path, missing exported
error policy and an uncaught JSON limit error. Independent regressions reproduced
them before the fixes and pass after integration. A separate source-derived BES
worker-start ownership race remains under review below.

The original panel, BES and Steam were not executed. Fixtures contain synthetic
configuration text in temporary directories. Qt checks run the reconstructed UI
with a simulated coordinator. A Windows binding check queries only the test
Python process, using read-only handles. No real window receives a message.
These checks do not establish original runtime parity or live Steam compatibility.

## Evidence and review scope

The bounded static specification dated 2026-10-13 describes the original video
helper at `0x1406787d0`, with the inequality branch at `0x14067a20f` and
`0x14067a255`. The global replacements use the panel text, vendor first and device
second. Their result is written to the panel template, leaving the Steam source
unwritten. This report includes no native dumps or proprietary template.

The reviewed reconstruction comprises:

- `src/fsm_panel/adapters/video.py`: validation, preview, transform, backup and apply.
- `src/fsm_panel/ui/video.py` and `src/fsm_panel/ui/window.py`: file selection,
  preview/apply reporting and shutdown.
- `src/fsm_panel/maintenance.py`: JSON output, expected hashes and error exit codes.
- [Video contract](video.md): intentional improvements and remaining boundaries.

The safe field mode, encoding limits, stale-preview checks, backups and atomic
replacement are new behavior. Tests of these safeguards do not attribute them to
the original application.

## Independent test coverage

[The QA tests](../tests/qa/test_offline_video.py) add 25 cases to the implementation
owner's video suite.

| Requirement | Independent check | Baseline result |
| --- | --- | --- |
| Recovered global replacement order | Explicit expected results for swapped IDs, equal template IDs, substring collisions, leading zeros and digits inside unrelated keys | Pass |
| Detect changed proposals | Metadata-only changes; changed bytes with the old size and timestamp; new file identity with old bytes and timestamp | Pass |
| Validate no-op proposals | A source comment change invalidates a preview even when both IDs still match | Pass |
| Repeat path and permission checks | Original-distribution marker appears after preview; template becomes read-only | Pass |
| Preserve source aliases | A read-only source with a second hardlink retains bytes, inode, timestamp and mode | Pass |
| Serialize local writers | Two threads applying one proposal produce one changed result and one stale error, with one exact backup | Pass |
| Report partial apply failures | CLI failures before replacement, after replacement and during rollback return exit 2, the usable backup and the correct rollback outcome | Pass |
| Preserve recovery reporting during cleanup failure | Failed replacement plus failed temporary-file removal still reports the retained backup | Failed at baseline; fixed in `f4542bf` |
| Bind CLI apply to source review | Changed source hash rejects an otherwise unchanged-ID apply | Pass |
| Exercise the real module entry point | Missing source returns JSON, exit 2, no traceback and no template mutation | Pass |
| Bind file selectors and previews | Source chooser changes the source field; canceled chooser keeps the current preview; changed selection discards a delayed result | Pass |
| Report UI rollback failure | UI shows failure and backup, suppresses success text and private injected diagnostics, and requires another preview | Pass |
| Finish active writes before closing | MainWindow waits for a real synthetic template replacement, keeps Qt responsive and joins the worker after backup and write complete | Pass |

## Finding V1: cleanup hides recovery metadata

Severity: P2. Reproducer:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_video.py::test_cleanup_error_does_not_hide_retained_backup_from_cli -q
```

The test injects two independent I/O failures: replacement raises
`PermissionError`, then removal of the remaining `.tmp` also raises
`PermissionError`. The original template and source remain intact. One complete
backup exists and contains the previous template bytes.

At the reviewed baseline, `apply_video_template` prepares `VideoApplyError` with
the backup and rollback result. Its `finally` block calls `staged.unlink`, whose
error overrides that structured exception. The CLI consequently returns only
`{"error": "video_file_unavailable"}`. The operator loses the known backup path
and rollback result. The UI's generic exception branch would also lose this
metadata; the independent regression reproduces the CLI path.

The expected result is exit 2 with `video_apply_failed`, `rollback_succeeded: true`
and the retained backup path. Cleanup must preserve the primary operation result.
A temporary file that cannot be removed may remain for later cleanup.

The implementation owner added a nonthrowing staging cleanup helper covering the
four removal paths in `f4542bf`. All 25 independent video cases pass after that fix.

## BES, CPU admission and Windows bindings

[The independent BES tests](../tests/qa/test_offline_bes.py) add 18 cases. The
adapter review used [its documented source contract](bes.md), the supplied BES
source, `src/fsm_panel/cpu.py` and the desktop composition. No C++ source or binary
is copied into these tests.

The native declaration review checked pointer-sized handles, message results and
signed 32-bit NTSTATUS values. Message declaration and failure behavior agree with
[Microsoft's SendMessageTimeoutW contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw).
Class 35 and the suspend-count type were checked against the
[System Informer native declarations](https://github.com/winsiderss/phnt/blob/master/ntpsapi.h).
Microsoft still documents [NtQueryInformationThread as an internal API that can change](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryinformationthread).

| Requirement | Independent check | Result through `91a3bdf` |
| --- | --- | --- |
| Recheck admission after waiting | During the initial lease request, account becomes STOPPING, runtime closes, simulation disables limits, or recovery becomes incomplete | Four regressions reproduced before the fix; pass after |
| Preserve original evidence | A matching mocked hash does not authorize an original-marked image or a hardlinked image | Two regressions reproduced before the fix; pass after |
| Keep recovery evidence after storage faults | Journal deletion fails after graceful exit; another recovery removes the record without sending another message | Pass |
| Commit cleanup state before exit | Persisting `resumed` fails; instance remains recorded, and no exit is sent until retry succeeds | Pass |
| Reject reused owner identity | Changed BES creation time with unknown target resume retains the session and sends no message | Pass |
| Require workers to finish after target exit | Missing or reused target does not authorize BES exit while added workers remain; reused target is never probed | Pass |
| Exercise real binding declarations without BES | Read-only guard, thread identity and zero-suspend-count queries inspect only the test Python process | Pass on this Windows host |
| Fail closed on native probe errors | Wrong thread owner, failed time query, negative status, wrong result length, nonzero count, changing snapshot and failed handle acquisition | Seven cases pass; every acquired synthetic handle closes |

Finding B1, P2: the runtime originally checked account and lifecycle state only
before awaiting admission. A stop or close during that wait still allowed a new
limit call. The fix rechecks all four conditions after admission returns. The
regression proves no command is delivered; a post-delivery error alone is not
sufficient.

Finding B2, P2: hash validation originally accepted a BES path within the marked
original distribution. The supplied component writes `bes.ini` beside itself.
The fix rejects an image beneath a resolved `Panel.exe` ancestor and rejects
hardlinked images. The tests use inert files and a synthetic digest; they never
launch the image. Renamed evidence without that marker and hostile namespace
changes remain outside this guard.

The integration owner clarified that owned game-target termination remains
available after a failed BES cleanup attempt. BES errors still propagate, and
its separate journal remains for retry. Generic termination never owns BES.
The documentation now reflects that decision. Source `hack.cpp:178–248,906`
shows a successfully opened target process handle retained through the worker's
lifetime, which protects that opened identity while generic target cleanup runs.

### Remaining worker-start ownership concern

The parser acknowledgement does not prove BES has opened the target. The supplied
`wndproc.cpp:406` creates its worker asynchronously; `hack.cpp:134` queries cached
process/thread pairs before the first `OpenProcess` at line 178. The adapter's
target guard ends after the acknowledgement. A target could exit after that
guard ends, and a reused PID could be opened by the delayed worker.

This is an inference from complete source paths, not a reproduced live race.
The integration owner accepted the issue and assigned an inherited, read-only
target handle to the dedicated BES child. That design can also retain the target
identity if the panel exits before BES. The change is pending implementation and
independent review. Original-process behavior still requires the stand; this issue
is not counted as resolved by the existing synthetic transport tests.

## Pure launch planning and proposal export

[The independent launch tests](../tests/qa/test_offline_launch.py) add 18 cases.
The review compared `launch_plan.py` and `launch_preview.py` against the three
sanitized launch preparation, loginusers preparation and userdata-ID reports.
Public evidence addresses and deliberate validation differences are recorded in
[the launch contract](launch-plan.md).

The recovered transformations remain deliberately literal. Tests check combined
Steam-only/update/NO_AVAST branches, ASCII-space splitting, preserved tabs and
zero-width text, broad Dota substitution before logfile/additional options,
substring suppression and negative/positive ties-to-even timestamps. No test
executes a proposed command or applies a loginusers/registry action.

Finding L1, P2: the CLI omitted `profile_directory_on_exception="propagate"` from
the exported proposal. It exported the shader group's catch-and-continue policy,
so a reviewer could not distinguish the initial directory action's failure path.
The fix exports that field. The regression checks both policies alongside a
missing-USERPROFILE shader result.

Finding L2, P2: a valid JSON numeric token containing 5,000 digits raised a plain
`ValueError` from Python's integer conversion limit. It bypassed the CLI's
content-free JSON error response. The fix preserves specific `LaunchPlanError`
codes, then converts other input `ValueError` failures to `launch_invalid_input`.
The regression fixes the conversion limit locally for repeatability, restores it
afterward, and checks that no output file is created.

Both regressions failed before `d262e64` and pass afterward. Additional export
checks confirm that invalid Unicode is rejected before output creation, concurrent
exports cannot overwrite one proposal, and a failed write cannot print success or
caller data. A failed write can leave a partial new output file; exclusive creation
will refuse that path on retry. No atomic-export guarantee is claimed.

## Reproduction environment and results

Windows, Python 3.14.6, PySide6 6.12.0, pytest 9.1.1. Dependencies were installed
with `uv sync --frozen --group dev` in the QA worktree. An import check confirmed
that `fsm_panel.__file__` belongs to that worktree's `src/fsm_panel` directory.

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.venv/Scripts/python.exe -m pytest tests/adapters/test_video.py tests/ui/test_video_panel.py tests/test_maintenance.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_video.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_bes.py tests/adapters/test_bes.py tests/test_cpu.py tests/ui/test_cpu_panel.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_launch.py tests/adapters/test_launch_plan.py tests/test_launch_preview.py -q
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_*.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_*.py
```

- Existing video suite: **81 passed, 4 skipped**. The skipped cases require
  symlink creation privileges. They are not counted as evidence of that OS path.
- Independent video baseline: **24 passed, 1 failed** at `791cd8a` (V1).
  After the fix: **25 passed**.
- Independent BES baseline: **12 passed, 6 failed** before `91a3bdf` (B1/B2).
  After the fixes: **18 passed**, with no expected-failure or skipped tests on this
  Windows host. Eight Windows ABI cases are explicitly skipped on other systems.
- Video QA, BES QA, BES adapter, CPU runtime and CPU UI together: **100 passed**
  through `91a3bdf`.
- The same checks plus the existing video adapter, UI and CLI suite:
  **181 passed, 4 skipped** through `91a3bdf`.
- Independent launch baseline: **16 passed, 2 failed** before `d262e64` (L1/L2).
  After the fixes: **18 passed**. Combined with the 99 existing launch cases:
  **117 passed** through `d262e64`.
- Ruff checks and formatting: **pass**.
- Pytest emitted an existing Starlette/httpx deprecation warning through the
  shared QA fixture imports; it did not affect the file or UI results.

Unresolved stand work includes original caller timing, launch/account integration,
real external application behavior, Windows policy variations and cross-process
or hostile filesystem races. The adapter explicitly does not claim a
cross-process transaction or a power-loss durability guarantee.
