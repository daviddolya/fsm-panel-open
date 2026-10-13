# BES ownership adapter

The adapter implements the supplied BES 1.7.9 source contract for a process already
owned by FSM Panel Open. The original executable was **not run** during development.
Synthetic tests verify our commands and lifecycle rules. They do not prove Windows
throttling, source-to-binary correspondence, original Panel invocation, or game parity.

## Integration contract

```python
from fsm_panel.adapters.bes import (
    BesJournal, OwnedBesService, ReviewedBesExecutable,
)

journal = BesJournal(data_directory / "bes.sqlite")
bes = OwnedBesService(repository, None, journal)

# owned_target must exactly match an existing repository OwnedProcess record.
session = await bes.limit(
    owned_target, 30,
    executable=ReviewedBesExecutable(reviewed_deployment_path),
)
assert session.state == "delivered"  # Message delivery, not measured CPU reduction.
await bes.unlimit(owned_target)       # Keep the dedicated instance for later use.
await bes.stop(owned_target.account_id)  # Stop workers, observe resume, then exit.
await bes.recover()                  # Retry every journalled instance after restart.
journal.close()                      # Only after pending service operations finish.
```

`limit(target, reduction_percent, *, executable=None)` accepts integer reductions
from 1 through 99. The per-call executable overrides the constructor configuration
for a **new** instance. An existing instance retains its recorded image identity.
It sends an explicit 100 ms cycle and zero initial delay, so those values do not
depend on an existing INI. These are new adapter choices, not recovered Panel defaults.

`verify_executable(ReviewedBesExecutable(path))` reads and hashes the file without
executing it. Only the reviewed supplied SHA-256 is accepted:

`8b10f04516ddfd970580b19171aa15f9205a8e0c12c4312a6d2a88f4a565eb1f`

Use a reviewed deployment copy in a writable directory, separate from the immutable
original evidence. BES reads and writes `bes.ini` beside its executable; changing
the working directory alone does not isolate its settings (`ini.cpp:30–107`).
The repository distributes neither BES executable nor C++ source.

`sessions(account_id=None)` returns immutable `BesSession` objects with `session_id`,
`target`, `instance`, `state`, `reduction_percent`, and `error_code`. Paths and process
identities in the journal are private operational data; exclude the journal from
exports, logs, and Git. `BesError.code` is safe for operator status; upstream text
is not exposed. Input validation uses the existing `ValidationError` codes.

The caller must serialize CPU commands with account lifecycle operations and call
`bes.stop(account_id)` **before** terminating that account's target processes.
Call `bes.recover()` before generic process recovery. If BES cleanup fails, retain
the account and present recovery; do not blindly continue with generic cleanup.
Admission and licence checks belong to the caller. Cleanup does not require a lease.

## Ownership and recovery

Each target receives a dedicated, initially idle instance launched with explicit
argv and `shell=False`: `--selfish --allow-multi --minimize`. No image-name or path
watching is used. The target must exactly match a repository record, including
its token/account, PID, creation time, and executable. The Win32 driver holds
read-only process handles and checks identity before addressing a window.

BES ownership is stored separately from the generic process controller, which can
force-kill processes. Before sending the first or updated PID command, the adapter
commits the instance identity, target identity, window handle, baseline BES thread
identities, and `unknown` state. A successful parser reply changes the state to
`delivered`. It cannot prove that a limiter worker started or achieved a CPU target.

Cleanup sends `WM_COMMAND/IDM_STOP` to the verified owned window. It then waits for
all BES threads added since the idle baseline to disappear and checks the owned
target's thread suspend counts without changing them. Only after those observations
does it mark `resumed` and send addressed `WM_COPYDATA/EXITNOW`. It waits for the
recorded process identity to disappear. `unlimit` performs the same stop and resume
checks but keeps the instance open. Repeated stop is safe.

The production probe dynamically calls `NtQueryInformationThread` with information
class 35 (`ThreadSuspendCount`), declared by the [System Informer native headers](https://github.com/winsiderss/phnt/blob/master/ntpsapi.h).
Microsoft documents that [NtQueryInformationThread may change or be unavailable](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryinformationthread).
Unsupported calls, denied access, changing snapshots, remaining workers, or nonzero
counts fail closed. No `SuspendThread`, `ResumeThread`, terminate, or kill call is
present in the adapter. A production stand must validate this read-only probe on
the supported Windows builds. The thread observations are a cleanup gate, not a
measurement of game responsiveness or successful throttling.

| State/error | Operator meaning and next step |
| --- | --- |
| `idle` | Owned instance recorded; no limit command has been sent. |
| `unknown` | A command may have been received. Recover before sending another limit. |
| `delivered` | BES acknowledged parsing the PID command; effect remains unmeasured. |
| `resumed` | Stop completed and zero suspend counts were observed after workers left, or no limit had been sent. |
| `cleanup_required` | Ownership retained. Retry `stop` or `recover` after resolving the error. |
| `bes_target_resume_probe_required` | Cannot safely observe the target before limiting. No instance is launched. |
| `bes_startup_timeout_cleanup_required` | Idle instance did not expose its expected window; ownership remains for retry. |
| `bes_resume_unconfirmed_cleanup_required` | No exit request is sent. Keep BES alive and investigate its window/permissions on the stand. |
| `bes_owner_lost_resume_unconfirmed` | Owned BES disappeared or PID was reused; target resume remains unconfirmed. No foreign process is signalled. |
| `bes_window_ownership_changed` | HWND no longer belongs to the recorded BES/class. No command is sent there. |
| `bes_exit_timeout_cleanup_required` | Resume was observed but exit was not. Retry graceful cleanup; no force-kill fallback exists. |
| `bes_journal_write_failed_cleanup_required` | Stop new work, restore storage, then recover. An idle spawn is retained in memory when initial persistence fails. |

Cancellation waits for the in-flight worker, retains the operation lock, and runs
cleanup for a cancelled limit before returning. Repeated cancellation cannot let a
new command race unfinished cleanup. Cleanup failure takes precedence over ordinary
cancellation so the caller receives an actionable error. Other accounts are still
attempted if one session fails during recovery.

A process crash between idle spawn and its first journal write can leave an idle
orphan. No target command is sent in that interval. A process crash after the
in-flight record is committed retains the information needed for recovery.
The journal holds a process-wide file lock to reject a second controller. Preserve
the journal until recovery succeeds; deleting it discards the ownership evidence.

## Source evidence

All locations below refer to the supplied `BES/src` tree. They are static source
observations; the original Panel-to-BES call and `BES_REDUCTION_PERCENT` mapping
remain unestablished. The SHA-256 values identify the exact inspected files.

| Source | Observation used by the adapter |
| --- | --- |
| `BattleEnc.h:21–27` | Window class `BATTLEENC`; source version text 1.7.9. |
| `BattleEnc.h:367–383` | COPYDATA identifier `0xBA1EC0DE`; four 32-bit fields followed by 1024 UTF-16 code units; 2064-byte payload. PARSE=`0xBECD0001`, EXITNOW=`0xBECD0000`. |
| `main.cpp:498–506,534–645` | `--allow-multi` permits another instance but does not bypass existing-window messages. `--selfish` bypasses that enumeration. CLI `--exitnow` can reach multiple instances. |
| `main.cpp:686–713` | Main window registers and uses `BATTLEENC`. |
| `misc.cpp:596–766,769–847,858–890` | Parser consumes an executable token, target/PID, percentage, and options. Help describes CPU reduction 1–99 and `N;C;D`. Adapter bounds are intentionally narrower than the parser's rounding/extended modes. |
| `BattleEnc.h:110–114` | Cycle 2–400 ms; initial delay 0–60000 ms. |
| `list.cpp:2986–3024` | Case-insensitive `PID:` prefix, decimal or hex DWORD parsing; invalid/overflow input rejected. Adapter emits decimal positive DWORDs only. |
| `list.cpp:3088–3135` | Existing PID updates parameters; an unknown PID falls into new limiting even when `bUnlimit` is true. Therefore the service never emits `PID:… --unlimit`. |
| `wndproc.cpp:792–866` | Receiver requires exact payload ID/size, copies the payload, parses commands; PID branch sets handled TRUE without checking `LimitPID`'s result. EXITNOW handler returns FALSE after synchronous stop and posting exit. |
| `resource.h:81,102`; `wndproc.cpp:1152–1177` | `IDM_STOP=30004`, `IDM_EXIT=30025`; stop clears active flags and waits via `WM_USER_STOP`. |
| `wndproc.cpp:428–505,1808–1888` | Stop waits can time out; window close can continue after semaphore failure. An acknowledged stop or graceful exit alone cannot prove resume. |
| `hack.cpp:659–824` | Duty cycle suspends target threads and attempts resume before checking the stop flag; resume errors remain possible. |
| `strings.utf8.h:389–396` | The component's notice identifies GNU GPL Version 2. The source header credits mion. The new Python adapter remains covered by this repository's licence; third-party packaging requires its own notices/source handling. |

| File | SHA-256 |
| --- | --- |
| `BattleEnc.h` | `fdcd6f2a7dad85d69002c397c6438a37ccf0e5e4dda8e2d2f74e7fbe1ef1890c` |
| `main.cpp` | `948af737e52facd3f4ab39bfea94bcc423a58846c752d7599f3fe6c49ad7fee7` |
| `misc.cpp` | `7d4c787ffdfa2b33e700cdd917708ca1cc1eec1134cf45f77afbbe4dd3a832b4` |
| `list.cpp` | `4f3f938ad4154d57f3062714d56d56c20924f2da6340f9b15185631e6f22cf99` |
| `wndproc.cpp` | `98fa0e116c695cfda77d9753975557382c10989aa1634f3ae971038d8c15be08` |
| `hack.cpp` | `ba2e9c042a9c51d2ff0a7d59c3305ecacd413b6bf968303db5b072aec5e4d3dc` |
| `ini.cpp` | `10121a298affa19a1c84b5356a564af507b2afb20101a0d0cf80ba318b0810f3` |
| `resource.h` | `6429fb0e8c4959172dd5419dbe90cbea160a888a81f654c8f25f2beccef2a301` |
| `strings.utf8.h` | `a0525334deda09c5619affde6eff44c67fdd097c4449695f3c3bf38c536f723d` |

The adapter assumes exclusive operation of its dedicated instance. `--selfish`
does not prevent another program from sending commands to it. Windows provides no
atomic HWND-ownership-check-and-send API; process/window identities are rechecked
immediately before commands, and handles are retained during an operation. These
limits, external BES interference, UIPI/elevation, worker timing, INI writes, and
real effect/resume measurements belong to the separate Windows stand gate.
