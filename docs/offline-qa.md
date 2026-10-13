# Independent offline reconstruction QA

Date: 2026-10-13. Video implementation reviewed at `791cd8a` in a separate QA
worktree. The reviewer owns the tests and this report, not the implementation.

The video checks found one recovery-reporting defect. A failed temporary-file
cleanup can hide the retained backup path after an unsuccessful replacement.
The implementation owner has prepared a fix; independent validation is pending.

The original panel, BES and Steam were not executed. Fixtures contain synthetic
configuration text in temporary directories. Qt checks run the reconstructed UI
with a simulated coordinator. They establish local file and UI behavior only.
They do not establish original runtime parity or live Steam compatibility.

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
| Preserve recovery reporting during cleanup failure | Failed replacement plus failed temporary-file removal still reports the retained backup | **Fail; finding V1** |
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

The implementation owner reported a nonthrowing staging cleanup helper covering
the four removal paths. This report does not count that change as verified until
the independent regression is rerun against the integration commit.

## Reproduction environment and results

Windows, Python 3.14.6, PySide6 6.12.0, pytest 9.1.1. Dependencies were installed
with `uv sync --frozen --group dev` in the QA worktree. An import check confirmed
that `fsm_panel.__file__` belongs to that worktree's `src/fsm_panel` directory.

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.venv/Scripts/python.exe -m pytest tests/adapters/test_video.py tests/ui/test_video_panel.py tests/test_maintenance.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_video.py -q
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_video.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_video.py
```

- Existing video suite: **81 passed, 4 skipped**. The skipped cases require
  symlink creation privileges. They are not counted as evidence of that OS path.
- Independent suite: **24 passed, 1 failed** at `791cd8a`. The failure is V1 and
  remains an ordinary regression test, without an expected-failure marker.
- Ruff checks and formatting: **pass**.
- Pytest emitted an existing Starlette/httpx deprecation warning through the
  shared QA fixture imports; it did not affect the file or UI results.

Unresolved stand work includes original caller timing, launch/account integration,
real external application behavior, Windows policy variations and cross-process
or hostile filesystem races. The adapter explicitly does not claim a
cross-process transaction or a power-loss durability guarantee.
