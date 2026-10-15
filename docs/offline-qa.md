# Independent offline reconstruction QA

Updated: 2026-10-15. Review started at `791cd8a`; fixes were independently checked
through CLI integration `44c79da`, BES planner `3caf37c`, launch-settings derivation
`c78fd79`, desktop config integration `11ac614`, GSI adapter `bdeedba` and GSI CLI
`3199344`, GSI desktop integration `90f9763`, Telegram transport `50c29ee`, and
inert autoaccept generators `0d7f2d1`, in a separate QA worktree.
The reviewer owns the tests and this report, not the implementation.

The video, BES and launch checks found five issues: lost recovery metadata, a
delayed admission race, an unguarded original-distribution path, missing exported
error policy and an uncaught JSON limit error. Independent regressions reproduced
them before the fixes and pass after integration. The review also identified a
source-derived BES worker-start ownership race and a child-identity loss during
handle cleanup. Both have integrated fixes and the bounded checks described below.
Config review found two further directory-transaction cleanup issues before the
first feature commit; independent regressions cover their integrated fixes.
CLI review then covered auxiliary-source ownership, text conversion, fingerprint
coverage, argument privacy and edits made during staging; their fixes pass too.

The original panel, BES and Steam were not executed. Fixtures contain synthetic
configuration text in temporary directories. Qt checks run the reconstructed UI
with a simulated coordinator. Windows checks use read-only handles to inspect the
test Python process and disposable Python children. No real window receives a message.
Telegram requests use synthetic HTTPX MockTransport handlers only; no network
connection or real message was sent. These checks do not establish original runtime
parity, live Steam compatibility or Telegram delivery.

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

[The independent BES tests](../tests/qa/test_offline_bes.py) add 25 cases. The
adapter review used [its documented source contract](bes.md), the supplied BES
source, `src/fsm_panel/cpu.py` and the desktop composition. No C++ source or binary
is copied into these tests.

The native declaration review checked pointer-sized handles, message results and
signed 32-bit NTSTATUS values. Message declaration and failure behavior agree with
[Microsoft's SendMessageTimeoutW contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw).
Class 35 and the suspend-count type were checked against the
[System Informer native declarations](https://github.com/winsiderss/phnt/blob/master/ntpsapi.h).
Microsoft still documents [NtQueryInformationThread as an internal API that can change](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryinformationthread).

| Requirement | Independent check | Result through `a9451cd` |
| --- | --- | --- |
| Recheck admission after waiting | During the initial lease request, account becomes STOPPING, runtime closes, simulation disables limits, or recovery becomes incomplete | Four regressions reproduced before the fix; pass after |
| Preserve original evidence | A matching mocked hash does not authorize an original-marked image or a hardlinked image | Two regressions reproduced before the fix; pass after |
| Keep recovery evidence after storage faults | Journal deletion fails after graceful exit; another recovery removes the record without sending another message | Pass |
| Commit cleanup state before exit | Persisting `resumed` fails; instance remains recorded, and no exit is sent until retry succeeds | Pass |
| Reject reused owner identity | Changed BES creation time with unknown target resume retains the session and sends no message | Pass |
| Require workers to finish after target exit | Missing or reused target does not authorize BES exit while added workers remain; reused target is never probed | Pass |
| Exercise real binding declarations without BES | Read-only guard, thread identity and zero-suspend-count queries inspect only the test Python process | Pass on this Windows host |
| Fail closed on native probe errors | Wrong thread owner, failed time query, negative status, wrong result length, nonzero count, changing snapshot and failed handle acquisition | Seven cases pass; every acquired synthetic handle closes |
| Preserve child identity through parent cleanup | Real STARTUPINFO layout with mocked handles; inheritance-flag reset succeeds or raises after child creation, followed by successful parent-handle closure | Both cases return the created child's identity |
| Recover a retained exited target | Identity lookup still sees the old object, but a verified guard reports exit; recovery does not probe target threads | Pass; stop and exit complete and the journal clears |
| Exercise actual Windows handle inheritance | Two disposable Python children, the driver's unmodified process-creation options, one extra inheritable process handle, and normal target exit | Child receives only the requested target pin, retains its identity after target exit, and the parent's copy closes |

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

### Finding B3: worker-start target ownership gap

The parser acknowledgement does not prove BES has opened the target. The supplied
`wndproc.cpp:406` creates its worker asynchronously; `hack.cpp:134` queries cached
process/thread pairs before the first `OpenProcess` at line 178. The previous
adapter's target guard ended after the acknowledgement. A target could exit after that
guard ends, and a reused PID could be opened by the delayed worker.

This is an inference from complete source paths, not a reproduced live race.
The integrated fix in `a9451cd` gives the dedicated idle BES child a verified,
read-only target handle through an explicit STARTUPINFO handle list. The parent
resets inheritance and closes only its own copy. The child's reference can retain
the target identity if the panel exits first. Legacy journal rows without evidence
of this pin remain available for cleanup but cannot accept new limit commands.

An independent Windows test substitutes a small Python probe at the Popen boundary,
preserving all process-creation options. The child receives the requested process
handle and excludes an unrelated inheritable process handle. After the parent's
copy closes and the target exits normally, the child's handle still yields the
original target PID and reports the process as signalled. Both disposable children
exit and all test-owned parent handles are released. This tests actual Windows
inheritance, without invoking BES or waiting for PID reuse.

The source review found no wholesale closure of inherited handles. Retention by
the exact shipped BES executable, including through its initialization and parent
failure, remains a stand requirement. The Python child test proves the Windows
transport behavior, not that external application behavior or complete parity.

### Finding B4: parent cleanup can hide a created child

Review of the first pin implementation found that an exception while resetting
the parent's inheritance flag could override a successful child creation. The
service would lose the child identity needed for its recovery journal. The fix
suppresses a reset-only error when the parent handle subsequently closes. If that
close also fails after child identity is known, a typed failure preserves the
identity and the service records the idle child as `cleanup_required` before
surfacing the error. No limit command is sent on that path.

The independent reset-failure regression passes; the implementation owner's
additional tests cover failed parent closure and subsequent journal recovery.
Failure to read a newly created child's identity, or a crash before journalling,
can still leave an idle child. This documented boundary does not authorize a
limit command or generic termination of that child.

### Legacy BES command and percent control

The later planner review at `3caf37c` compared `bes_plan.py` against the complete
sanitized consumer report for `0x1409572d0` and its daemon-thread wrapper. The
actual command targets `game/bin/win64/cs2.exe` and the Steam CEF web helper.
The shorter game path, overlay target and literal 50 values belong to debug
output. The planner keeps those two outputs distinct, preserves the unquoted
executable path and literal backslash operations, and does not clamp or invert
integer percentages. It has no executor.

Two independent cases preserve trailing separators, embedded quotes, relative
components and forward-slash Steam input exactly. A Qt check confirms fallback
25, literal accepted settings, rejection of invalid settings without changing
the current form value, and disabled import while busy. Reading a percentage
submits no process operation. No material discrepancy was found. The live owned-PID
adapter remains a deliberate change from the original's executable-path targets.

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

## Isolated account config and autoexec copies

[The independent config tests](../tests/qa/test_offline_configs.py) add 30 cases.
The review compared `account_config.py`, `autoexec.py`, and the five packaged
generic templates with the sanitized config-writer and autoexec specifications
dated 2026-10-13. The [public contract](account-config.md) records native evidence
addresses, exact template hashes, APIs, deliberate changes and remaining limits.

The pure plans describe recovered order and exception scopes. Their handler
descriptions are not an executable simulator of the original. The separate writer
requires an adapter-created marker and fixed paths beneath a new offline workspace.
Whole-tree staging, backups, stale-preview validation and explicit failures are
new safeguards, replacing the original's partial writes and swallowed errors.

| Requirement | Independent check | Result at `2448aae` |
| --- | --- | --- |
| Preserve force semantics and effect scope | Both force values perform the same removals/copies; only logs differ. Another account, source files and game cfg stay unchanged; backup is the exact previous account tree | Pass |
| Preserve literal localconfig predicates | None, empty string and false mapping values select distinct branches; NO_AVAST prevents restoration; a non-string restore cannot truncate a copied file | Pass |
| Preserve equal autoexec and legacy file | CRLF/CR/LF sources compare after text decoding; equal content creates no stage, backup, write or deletion and retains file identity/timestamps | Pass |
| Revalidate no-op proposals | Source, destination, unrelated legacy file and workspace marker changes each invalidate an otherwise equal-content preview | Pass |
| Enforce containment | Traversal/reserved/stream filenames, a hardlinked unrelated account member and a real Windows junction inserted after preview are rejected without external mutation | Pass |
| Recover directory moves | I/O or stale-read failures after moving the original either restore its exact contents or return the retained backup with failed-rollback status | Four cases pass |
| Preserve an independent replacement | A new target created after original-to-backup movement is never overwritten by rollback | Pass; original remains in reported backup |
| Preserve cleanup outcomes | Failed stage removal retains primary error and stage path; KeyboardInterrupt restores the original before propagating | Pass |
| Own every cleanup target | A pre-existing stage directory survives a failed reservation unchanged | Pass |
| Serialize simultaneous applies | Two autoexec applies from one preview produce one successful change, one stale rejection and one exact backup | Pass |

Finding C1, P2: the first transaction implementation rethrew every
`ConfigStalePreviewError` without attempting rollback. Revalidation during the
second directory move can raise that error after the original has already moved
to backup, leaving the target missing and hiding its backup path. The fix bypasses
rollback only for stale errors before original movement. The independent tests
inject the late error with both successful and denied rollback, checking exact
restoration or structured recovery metadata.

Finding C2, P2: cleanup originally ran even if stage creation failed because the
random stage path already existed. That could remove a directory this attempt
did not create. Natural UUID collisions are unlikely; the regression fixes the
generated name to a pre-existing synthetic tree to test the ownership invariant.
The fix reserves the directory first and records successful creation before
allowing cleanup. The pre-existing tree retains its bytes, identity and timestamp.

Both findings were accepted and fixed before `2448aae`. The two renames still
leave a brief missing-target interval and are not a crash-safe transaction.
Hostile concurrent namespace changes, NTFS metadata beyond the documented copy
policy and live Steam integration remain outside these offline tests. These tests
cover the source API; command-line integration is reviewed below.

## Config CLI inputs, fingerprints and failures

[The independent CLI tests](../tests/qa/test_offline_config_cli.py) add 27 cases.
The review started at `e814fe0`, with fixes checked at `64582b4` and `44c79da`.
The [operator guide](config-tools.md) documents the isolated workspace, required
inputs, review fingerprint and backup recovery fields.

Finding C3, P2: settings and localconfig-copy inputs bypassed the adapter's source
guard. Placing either input at a generated cfg output path let apply overwrite its
own input. The fix reads both through the shared guarded loader. Two independent
cases now reject overlap without changing the workspace.

Finding C4, P2: the localconfig copy used raw decoding, retaining CRLF, before
Windows newline translation at output. The result contained CRCRLF. The shared
loader now applies universal newline decoding; the regression preserves the
expected CRLF output and leaves the input bytes unchanged.

Finding C5: the first fingerprint omitted auxiliary file snapshots. Changing
unused settings or file metadata could leave the same token. The stronger review
contract now includes their complete raw hash and file identity, while displaying
only the digest. Regressions check stable unchanged previews, changed unused
settings, and same-text files with changed timestamps or replaced identities.
The token remains a correlation value, not authorization or a signature.

Finding C6, P2: argparse printed malformed numeric arguments verbatim on stderr.
A synthetic private sentinel supplied as `--timestamp` reproduced the disclosure.
The parser now returns a stable JSON error with exit 2. Module-entry tests confirm
that the value is absent, stderr is empty, and ordinary `--help` still exits 0.
Malformed settings and malformed marker JSON also produce content-free errors.
The nested marker fixture was already rejected by the baseline on this Python
build; it does not claim a reproduced recursion failure.

Finding C7, P2: auxiliary inputs were checked before apply but not during its
directory staging. Editing settings or the localconfig copy after the top-level
copytree completed still committed the old proposal. The fix retains immutable
auxiliary snapshots in `AccountConfigPreview`, so every repeated validation,
including the final check before directory movement, rereads their identity and
raw hash. Both staging-edit regressions now reject without changing the target.

Additional cases check changed effective settings/login/force and destination
contents; stale no-op fingerprints; exact no-op file identities and timestamps;
successful backups; and rollback success/failure responses. Failed apply returns
no success fields and reports the usable retained backup when rollback fails.
All commands operate only on synthetic files, and no proposed command is executed.

## Config controls in the shared desktop

[The independent desktop tests](../tests/qa/test_offline_config_ui.py) add 19 cases
against the integrated `MainWindow` at `11ac614`. They instantiate the real Qt
controls and async bridge, use temporary account/config trees, and inject failures
at filesystem or unrelated-cleanup boundaries. The coordinator is a simulator;
these checks do not launch Steam or the original application.

| Requirement | Independent check | Result |
| --- | --- | --- |
| Keep explicit import ownership | The page shares the window's bridge, requires its own settings import, uses that import even when main-window settings disagree, and leaves both settings sources unchanged | Pass |
| Revoke obsolete previews | Editing account, autoexec, settings or workspace input after both previews disables both applies and leaves the tree unchanged | Four cases pass |
| Bind pending imports to their selected input | Change the selection after an actual read but before completion; the old snapshot cannot enable preview, and a new import determines the later localconfig behavior | Pass |
| Isolate shared-bridge errors | An unrelated failed operation cannot release the config page's pending-work lock, and its private exception text is absent from the window log and status | Pass |
| Recheck source identity | Replace settings, localconfig, video or machine input with identical bytes and timestamps but a new file identity; all four old previews are rejected | Pass |
| Preserve a concurrent target edit | Change the account tree after its staging copy; apply is rejected, the external edit survives, and no backup or stage remains | Pass |
| Report actual recovery state | Deny the stage-to-target rename with successful or denied rollback; UI reports the correct status and retained exact backup without private exception text or success wording | Two cases pass |
| Finish file work before window shutdown | Pause actual tree staging, close twice and check a queued Qt timer still runs; release with unchanged or edited source, with and without an earlier license-cleanup error | Four cases pass |
| Preserve shared bridge ownership | Closing just the config page leaves another simulated account operation usable; closing MainWindow then stops the account and joins its bridge | Pass |

The shutdown cases check the coordinator thread and the recorded executor thread
have both exited after their real file operation finishes. Successful staging
retains the exact previous tree; a stale source leaves the target unchanged. A
license-cleanup error remains a content-free shutdown diagnostic and does not
skip config cleanup. No additional production defect was reproduced in this pass.

## Recovered launch-setting derivation

[The independent derivation tests](../tests/qa/test_offline_launch_settings.py) add
44 cases against `c78fd79`, reviewed with the sanitized native derivation
specification dated 2026-10-13. The bounded initializer is at `0x1406b0d40`;
resolution loading, split, conversions and fallback are anchored at
`0x1406bb662`, `0x1406bba60`, `0x1406bbc8d`/`0x1406bbcfc` and `0x1406bc0b6`.
Steam-only and shader fallbacks are at `0x1406bd337` and `0x1406c2f63`; BES's
integer 25 fallback is at `0x1406b2e28`.

The checks cover all 16 combinations of missing keys, preserving explicit false
values and producing updates only for absent keys. They independently cover
partial dimension failures resetting both values, preservation of malformed
source values, the active Python digit-conversion bound, nested value ownership,
and rejection of selected non-JSON values. The proposal integration checks the
effects of raw JSON truthiness on game arguments and the inverted shader branch,
including accepted negative/zero dimensions and the missing-key defaults. Each
derived field is rejected when also supplied explicitly, even if its value is
false. Invalid legacy objects produce a content-free error and no output file.

Input bytes stay unchanged; private settings and inherited environment sentinels
are absent from public summaries and the derived-settings metadata. Nested
results are detached copies, not immutable containers. The bounded loader is
pure and does not persist default updates. No additional production defect was
reproduced. These tests do not establish the full initializer, original runtime
parity, real launch behavior, or downstream acceptance of dimensions by a game.

## GSI writer, selector and offline CLI

[The independent GSI tests](../tests/qa/test_offline_gsi_config.py) add 39 cases.
The review compared `bdeedba` and its `3199344` CLI integration with the sanitized
GSI writer specification dated 2026-10-13. The native writer slice is
`0x14097da31..0x14097e519`; the selector/server helper spans
`[0x140a024c0, 0x140a041aa)`. Only the sanitized template and specification were
used. No authentication literal from the original was read or committed.

| Requirement | Independent check | Result |
| --- | --- | --- |
| Preserve the exception-based selector | Enumerate all 1,024 ten-boolean sequences; choose the first exception outcome or retain 6969/1 after ten completed expressions; ignore the eleventh outcome | Pass |
| Do not reinterpret HTTP status | Reject supplied numeric statuses 200/404/500 when consumed; ignore them after an earlier exception outcome; no port or socket is allocated | Pass |
| Preserve literal rendering | Replacement is all-occurrence and nonrecursive; token markers remain inert in the legacy plan; port and filename index stay independent; Windows path text retains `..` | Pass |
| Preserve evidence identity | Packaged sanitized template matches the independently recorded 1268-byte export and fixed SHA-256, with one marker of each kind | Pass |
| Reject VDF structure injection | Multi-line quote/brace/control payloads never create output or disclose the caller token in errors; allowed placeholder text inside a caller token is retained literally | Pass |
| Preserve previous indices | Two applies with different indices and tokens keep the previous indexed file and exact complete backups; unrelated bytes survive | Pass |
| Bind hash and identity snapshots | Same-size edits with preserved timestamps/inodes, identical replacements of target files or the cfg directory, a source hardlink added after preview and a token identity replacement during staging all reject the old preview | Pass |
| Recover a late stale error | A stale error after moving the original tree either restores it exactly or returns its exact retained backup and failed-rollback metadata | Two cases pass |
| Serialize overlapping writers | Pause a real GSI staging copy, submit an autoexec apply from the same old snapshot, then release; GSI commits once and the waiting autoexec fails stale without overwriting it | Pass |
| Bind CLI review fingerprints | Changed token, raw line ending, source/target identity, unrelated tree bytes, port, index or encoding rejects the reviewed digest without a write | Eight cases pass |
| Keep CLI diagnostics private | Actual module-entry numeric parse errors, structural token errors, source/output overlap and both rollback outcomes return content-free errors and no success fields | Pass |
| Recheck after fingerprinting | Replace the token immediately after computing the CLI fingerprint; adapter validation still rejects before staging | Pass |

No additional production defect was reproduced. The selector tests establish
the pure supplied-outcome transformation, not availability of any port or live
listener behavior. The filesystem tests use synthetic temporary trees and local
Python only. They do not validate the original GSI HTTP handler, live game ingest,
writer/listener ordering, full GUI entry conditions, cross-process transactions
or power-loss recovery. The new GSI GUI is reviewed separately below.

## GSI controls in the shared desktop

[The independent GSI desktop tests](../tests/qa/test_offline_gsi_ui.py) add 21 cases
against `90f9763`. Unlike the implementation-owner tests' lightweight owner, these
checks instantiate MainWindow with its shared bridge and both real config panels.
All filesystem operations use synthetic temporary data; the coordinator is a
simulator and no network operations are performed.

| Requirement | Independent check | Result |
| --- | --- | --- |
| Keep workspace selection explicit | A workspace created on the config page is not silently selected by GSI; the same bridge serves both, and explicit GSI apply ignores a different global game path | Pass |
| Recheck the reviewed selection | Suppress Qt change signals while editing each of the five inputs; apply still invalidates the review without submitting work or writing files | Five cases pass |
| Recheck a pending selection | Change the index without an edit signal after reading the real preview but before returning it; completion cannot enable apply | Pass |
| Isolate shared-bridge errors | An unrelated failed task neither completes the pending GSI operation nor exposes its private exception text in the window log or panel | Pass |
| Bind file identities | Replace the token or selected target with identical bytes and timestamps; displayed previews are rejected before any write | Two cases pass |
| Preserve cross-page review safety | Apply GSI then autoexec, or autoexec then GSI; the second page's old preview fails stale and cannot overwrite the first result | Two cases pass |
| Display actual recovery state | Deny install, optionally deny rollback and stage cleanup; status reports only paths that remain, exact old-tree backup/restoration, no success wording and no token/error contents | Four cases pass |
| Wait for real GSI staging | Close twice during a real tree copy with unchanged or edited token, and with or without failure of earlier config-page cleanup; Qt remains responsive and both coordinator/executor threads exit | Four cases pass |
| Drain simultaneous config writers | Start GSI staging and queue an autoexec apply, then close MainWindow; GSI commits once, autoexec reports stale, one exact backup remains and shutdown completes | Pass |

Successful previews and writes expose neither token bytes nor token-source paths
in summaries, logs or status. Recovery text uses Qt plain text. The four failed
install cases inspect the actual retained backup and stage trees rather than
substituting a preconstructed error result. No additional production defect was
reproduced. This is behavioral Qt integration evidence, not visual parity,
original-app execution, a live GSI receiver test or proof of game compatibility.

## Telegram legacy contracts and owned transport

[The independent Telegram tests](../tests/qa/test_telegram_boundaries.py) add 60
cases against `50c29ee`, the integrated `b296a0a` author checkpoint. The review
used the sanitized static Telegram specification dated 2026-10-15 and the
[public contract](telegram.md). It did not read original bot tokens or raw native
constants. Every HTTP operation uses an owned MockTransport with synthetic data.

| Requirement | Independent check | Result |
| --- | --- | --- |
| Preserve literal legacy proxy rules | Seventeen fixtures cover numeric-port precedence around `@`, reversed credentials, ignored third component, colon-containing passwords, empty credentials, raw schemes, unvalidated ports and bracketed address differences | Pass |
| Preserve split and return differences | Threaded components retain empty/raw entries and require exactly two colon parts; receipt components strip/filter and split once; falsey non-None IDs remain receipts | Pass |
| Preserve all eight request shapes | Form fields, raw HTML, distinct topic/reply fields, scalar timeouts, disabled legacy TLS flag and repeated path-photo names/MIME are checked with file/socket access forbidden | Pass |
| Preserve event and fallback quirks as pure data | Inactivity matching is case-sensitive; truthy repeating bypasses suppression; state updates precede delivery; important-filter expansion and empty substrings retain their rules; a normal None fanout return can request a producer repeat | Pass |
| Retain uncertain delivery outcomes | Malformed replies, non-Boolean success, unusable IDs, wrong edit IDs, redirects, server errors and post-header connection exceptions remain uncertain; explicit rejection stays rejected | Pass |
| Avoid implicit retries and fallback | Each ambiguous photo operation makes one request, including write/read/protocol failures and deadline expiry; explicit rejection does not send text; two explicit identical calls remain two separate attempts | Pass |
| Own upload handles | Failure opening a second file closes the first without dispatch; ambiguous multipart sends close both files and expose neither original filename in the multipart body | Pass |
| Join repeated cancellation | Cancel a request twice during delayed EOF cleanup, then close its client; both await cleanup, transport close follows stream close, and cancellation retains the validated message ID | Pass |
| Preserve close ownership | Cancel a close waiter twice and verify cleanup still finishes before cancellation propagates; closing one client leaves a second client's pending request usable | Pass |
| Bound streamed responses | Exactly 64 KiB of valid JSON plus whitespace is accepted; one byte more ends body iteration and closes the response without a second request | Two cases pass |
| Keep diagnostics private | Sensitive synthetic values remain absent from request/cleanup logs, results, reprs and formatted close-error traceback; unrelated task logs remain observable | Pass |

Pre-commit source review discussed HTTPX EOF auto-close cancellation and
post-header connection-error classification. Both safeguards were present in the
first stable snapshot executed by this reviewer; no pre-fix runtime reproduction
is claimed. No additional production defect was reproduced at that checkpoint.
An injected response-close failure retains an already validated delivery result;
an injected transport-close failure remains a content-free configuration error
and does not falsely report a closed client.

The strict new transport intentionally does not execute the legacy fallback,
daemon fanout or producer-repeat policies. These tests establish local request,
result and ownership behavior; they do not establish live Telegram acceptance of
repeated photo parts, proxy compatibility, original runtime parity or actual
delivery. GUI integration is outside this backend review.

## Inert autoaccept generator boundaries

[The independent autoaccept tests](../tests/qa/test_autoaccept_boundaries.py) add
47 cases against author commit `0d7f2d1`, locally applied as `1bcf9f7`. The review
used sanitized input-automation checkpoint 02, dated 2026-10-15, and the
[public trace contract](autoaccept-plan.md). Native address comparison is a
separate review. These tests supply all times, settings, rows, windows, packed
message values and effect acknowledgements; they execute no input or co-op calls.

| Requirement | Independent check | Result |
| --- | --- | --- |
| Preserve the normal dispatcher schedule | Negative/zero delays are omitted; positive subnormal and very large integer delays are retained; four batches, nested lock descriptions and the original pre-lock start time remain ordered | Five cases pass |
| Preserve late short-circuit decisions | Expand the row helper before batch three or four; a true result ends the dispatcher without any further abandonment reads or side effects | Two cases pass |
| Preserve alternative behavior | A large initial delay remains unused; only the pixel call's ordinary exception is caught; traceback or unblock errors terminate without invented finally cleanup | Pass |
| Read all rows at index six | Twelve count/mode combinations distinguish equality, thresholds two/six and deferred mode reads; opaque other columns remain untouched; malformed final rows still fail after six qualifying rows | Pass |
| Preserve whole-set abandonment | None skips all reads; exact singleton returns false; a superset abandons, optionally leaves, then refreshes the set after requesting the supplied server's short ID | Pass |
| Preserve abandonment log arguments | Sort the converted short IDs lexically while retaining duplicates; errors at every dependency prevent a true return and all subsequent requests | Pass |
| Preserve each background message | Duplicate windows and zero/negative handles remain in the supplied shuffle; per-window mode reads choose coordinates; nine separate packed values reach their respective messages, including literal None for button-up | Pass |
| Preserve the batch exception boundary | Inject an ordinary failure at every yielded request; errors within the batch skip its whole remainder and describe only unblocking, while either block-call failure propagates | Pass |
| Preserve timeout and already-polled state | Empty early matches do not consume polling; the first window times out and still clicks; duplicate windows and a later account receive clicks without another poll | Pass |
| Preserve deferred foreground observations | Account title is read only after earlier clicks; activation/delay precede left/top/width/height reads; each click gets fresh mode/CS_HEIGHT, and width remains unused | Pass |
| Preserve pixel arithmetic and wall-clock decisions | Green 88 does not match and 89 does; other channels are ignored; a backward clock step permits another sample, the found notice can exceed seven seconds, and sample/click heights remain separate | Pass |
| Preserve terminal failures and activation retries | Foreground dependency errors end remaining windows without release/unblock requests; mixed find/foreground/restore failures retry three times and delay after the last failure | Pass |
| Keep traces inert when closed or interrupted | Real file open, wall clock, sleep, shuffle and socket entry points are forbidden; closing at every yield emits no cleanup; protocol errors and BaseException bypass ordinary-error handlers | Pass |

No production defect was reproduced. The tests retain the recovered cleanup gaps
as trace behavior; they do not run those effects. Frozen trace records, strict
observation types and content-free protocol errors are deliberate reconstruction
boundaries. The generators do not prove window ownership, successful focus,
library conversion of None wParam, actual game input, scheduler cadence or
original runtime parity. No executor or GUI connection is covered by this slice.

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
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_configs.py tests/adapters/test_account_config.py tests/adapters/test_autoexec.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_config_cli.py tests/test_config_cli.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_bes.py tests/adapters/test_bes.py tests/adapters/test_bes_plan.py tests/test_cpu.py tests/ui/test_cpu_panel.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_config_ui.py tests/qa/test_offline_launch_settings.py tests/ui/test_config_files.py tests/ui/test_window.py tests/qa/test_gui_shutdown.py tests/adapters/test_launch_settings.py tests/test_launch_preview.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_gsi_config.py tests/adapters/test_gsi_config.py tests/test_gsi_cli.py tests/test_config_cli.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_offline_gsi_ui.py tests/ui/test_gsi_config_panel.py tests/qa/test_offline_config_ui.py tests/ui/test_window.py tests/qa/test_gui_shutdown.py -q
.venv/Scripts/python.exe -m pytest tests/qa/test_telegram_boundaries.py tests/notifications -q
.venv/Scripts/python.exe -m pytest tests/qa/test_autoaccept_boundaries.py tests/adapters/test_autoaccept_plan.py tests/adapters/test_input_plan.py -q
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_video.py tests/qa/test_offline_bes.py tests/qa/test_offline_launch.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_video.py tests/qa/test_offline_bes.py tests/qa/test_offline_launch.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_configs.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_configs.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_config_cli.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_config_cli.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_config_ui.py tests/qa/test_offline_launch_settings.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_config_ui.py tests/qa/test_offline_launch_settings.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_gsi_config.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_gsi_config.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_offline_gsi_ui.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_offline_gsi_ui.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_telegram_boundaries.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_telegram_boundaries.py
.venv/Scripts/python.exe -m ruff check tests/qa/test_autoaccept_boundaries.py
.venv/Scripts/python.exe -m ruff format --check tests/qa/test_autoaccept_boundaries.py
```

- Existing video suite: **81 passed, 4 skipped**. The skipped cases require
  symlink creation privileges. They are not counted as evidence of that OS path.
- Independent video baseline: **24 passed, 1 failed** at `791cd8a` (V1).
  After the fix: **25 passed**.
- Independent BES baseline: **12 passed, 6 failed** before `91a3bdf` (B1/B2).
  After those fixes: **18 passed**. With the target-pin additions through
  `a9451cd`: **22 passed**, with no expected-failure or skipped tests on this
  Windows host. Eleven Windows ABI/inheritance cases are skipped on other systems.
- BES QA, BES adapter, CPU runtime and CPU UI together: **96 passed** through
  `a9451cd`, including the real Windows inheritance test.
- With the later BES planner and percent-control additions: **25 independent
  cases passed**, and **112 passed** across BES QA, adapter, planner, CPU runtime
  and CPU UI at `3caf37c`.
- Video QA, BES QA, BES adapter, CPU runtime and CPU UI together: **100 passed**
  through `91a3bdf`.
- The same checks plus the existing video adapter, UI and CLI suite:
  **181 passed, 4 skipped** through `91a3bdf`.
- Independent launch baseline: **16 passed, 2 failed** before `d262e64` (L1/L2).
  After the fixes: **18 passed**. Combined with the 99 existing launch cases:
  **117 passed** through `d262e64`.
- Independent config/autoexec cases: **30 passed** at `2448aae`. Combined with
  the 91 implementation-owner cases: **121 passed**. The junction check used
  a real synthetic Windows junction; it is skipped on other operating systems.
- Initial CLI review: **15 passed, 5 failed** at `e814fe0`. All original 20 cases
  passed after `64582b4`; two added staging-edit cases then exposed C7.
  After `44c79da` and five further identity/help cases: **27 passed**.
  Config API and CLI suites together: **153 passed**.
- Desktop config integration and launch-settings derivation: **63 new independent
  cases passed** at `11ac614`, and **152 passed** when combined with author config
  UI, MainWindow, GUI shutdown, derivation and proposal tests. No skips or expected
  failures occurred in this focused run.
- GSI adapter and CLI: **39 new independent cases passed** at `3199344`;
  **158 passed, 2 skipped** together with the author GSI adapter and both config
  CLI suites. The two author-suite symlink cases require privileges unavailable
  on this Windows host; they are not counted as verified OS behavior.
- GSI desktop integration: **21 new independent cases passed** at `90f9763`;
  **74 passed** together with the author GSI GUI suite, independent config-page
  suite, MainWindow and GUI shutdown checks. No skips or expected failures.
- Telegram backend: **60 new independent cases passed** at `50c29ee`;
  **173 passed** together with the author's pure legacy and transport suites.
  No skips or expected failures; every request used MockTransport.
- Telegram desktop and vault upgrade: **33 independent cases passed** after
  `9b1f229`. Review found cancellation/known-receipt loss, omitted proxy import,
  old-vault incompatibility and nested unsealed v1 secret acceptance. Root fixes
  were independently verified; the latter reproduced as two failures before
  `9b1f229`. Actual MainWindow shutdown joins notification cleanup even after a
  trade cleanup failure. All request traffic is synthetic MockTransport.
- Integrated run at `fe25337`: **1382 passed, 6 skipped, 6 PostgreSQL deselected**.
  The complete run first exposed Alembic disabling existing HTTP loggers;
  `ecf5bb9` fixes the logging boundary. Scoped Telegram privacy checks still
  assert that unrelated logging remains available.
- Inert autoaccept generators: **47 new independent cases passed** at `0d7f2d1`;
  **143 passed** together with the author autoaccept and existing input-plan
  suites. No skips or expected failures; all effects remained inert requests.
- Ruff checks and formatting: **pass**.
- Pytest emitted an existing Starlette/httpx deprecation warning through the
  shared QA fixture imports; it did not affect these results.

Unresolved stand work includes original caller timing, launch/account integration,
real external application behavior, Windows policy variations and cross-process
or hostile filesystem races. The adapter explicitly does not claim a
cross-process transaction or a power-loss durability guarantee.
