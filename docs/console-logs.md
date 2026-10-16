# Read-only console-log discovery

`fsm_panel.adapters.console_logs.discover_console_logs` finds recent console-log
files in explicitly selected local copies. It reads directory entries and file
metadata only: no contents, drive enumeration, game processes, account state,
network, timers or writes. It is not yet connected to autoaccept or the GUI.

```python
from pathlib import Path
from fsm_panel.adapters.console_logs import discover_console_logs

result = discover_console_logs(
    Path(r"C:\analysis-copy\flat-logs"),
    [Path(r"C:\analysis-copy\sandbox-copy")],
    ["synthetic_account"],
    max_age=120.0,
)
if not result.complete:
    print(result.issues)  # Fixed codes only, no paths or account names.
# result.logs contains sensitive local paths and logins; do not log/export it
# into public research reports. A complete discovery is not a game-state result.
```

The account names and roots are explicit snapshots. There is no automatic read
of saved sessions or account files. Up to 50 login strings and 32 sandbox roots
are accepted. A missing root is reported, while other roots can still be scanned.
An empty successful result does not establish that every account is in a menu,
disconnected, or safe to accept.

## Recovered selection rules

Source: `Panel.exe` SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`,
`HXgDoEKRWiOp(self, path, save_to_var=False)` at `0x140b20930`, 16,166 bytes,
descriptor line 33242. The private sanitized contract has SHA-256
`613bc607ec1b7543ff884073c017fb47988f9f068268ec282780818f9f28ca60`.
Default registration is bounded by `0x140c21385`–`0x140c213a9`.

| Rule | Flat logs | Sandbox logs |
| --- | --- | --- |
| Directory scope | Immediate children of the supplied path | Descendants whose Windows-style path contains the case-sensitive substring `Counter-Strike Global Offensive\game\csgo` |
| Filename | Ends with `.log` | Contains `.log` anywhere |
| Login | Remove **every** `.log` occurrence; compare case-sensitively with launched logins | Same |
| Time | `mtime > now - max_age` | Keep unless `now - mtime > max_age` |
| Exact age boundary | Excluded | Included |

The clock is read separately for each matching regular file. Future timestamps
are accepted by both recovered comparisons. There is no sorting or result
deduplication; explicitly overlapping roots can yield repeated paths. The
underlying filesystem chooses enumeration order. The sandbox path check is a
substring check, not exact component matching. Forward separators are translated
to backslashes for this check on non-Windows test hosts.

## Deliberate reconstruction changes

The original reads live `launched_accs` and `AUTOACCEPT_READ_TIME`, obtains drives
from another helper, deduplicates those drives with `list(set(...))`, and builds
each sandbox path with raw `disk + 'avast! sandbox'`. It can include directories
whose names look like logs. Its flat failure handler abandons the remaining flat
files; sandbox errors have separate per-disk and outer catch scopes. It updates
`flat_logs`/`avast_logs` before appending paths and can assign the returned list to
`consolefiles`. Those state mutations and silent exception semantics are **not**
executed by this new utility.

The new API takes explicit sandbox directories and immutable login/time-window
snapshots. It requires regular files, nonnegative finite age, and reports partial
scans using fixed issue codes. It skips symbolic links and Windows reparse
points, including selected-root ancestors. It rechecks metadata before using a
candidate and rechecks ancestors before scanning a sandbox directory. It does
not inspect UNC/device/URL roots or Windows drives classified as remote: locality
is checked before any filesystem metadata access. Local paths with reserved or
alternate-stream syntax on Windows are also rejected. It does
not claim an atomic snapshot or protection against hostile concurrent filesystem
replacement. Use copies under the operator's control.

The default total entry budget is 20,000; each file/directory entry counts even
when its name is irrelevant. The maximum configurable budget is 1,000,000. A
folder's entries are collected before candidates are evaluated; hitting the
budget discards that unfinished folder and ends the entire scan. The default
depth limit is 40 (configurable 0–100, root depth zero). An optional
`threading.Event` supports cooperative cancellation between filesystem operations;
it cannot interrupt an OS call that is already blocked. Previously completed
results remain available with an incomplete status.

Issue codes: `root_unavailable_or_unsafe`, `directory_unavailable`,
`entry_unavailable`, `linked_entry_skipped`, `entry_changed`, `entry_limit`,
`depth_limit`, `cancelled`. Invalid arguments or a non-finite clock observation
raise `ConsoleLogInputError('invalid_console_log_input')`.

## Verification limits

Synthetic filesystem tests cover both age boundaries, names/case, substring
paths, no content reads, per-file time, repeated roots, partial scans, budgets,
reparse-ancestor rejection and cancellation. These prove the new utility's
contract, not the original process or Windows-stand parity.

The real content reader is `oAjysWzxGvAh(self, path, linecount=1)` at `0x140b2a590`,
33,166 bytes. Discovery does not implement that parser. The recurring autoaccept
scanner is at `0x140b48440`; its trigger/state/error contract is being recovered
separately. No result of this utility can directly authorize mouse/keyboard input.
