# Offline account config and autoexec preparation

The adapters implement pure plans for the recovered account-config writer and
autoexec helper, plus a separate writer restricted to a newly created offline
workspace. They never execute Panel, Steam, CS2 or BES. Synthetic filesystem tests
verify these adapters; they do not establish live game or launch parity.

## Evidence and public templates

Static target: Panel.exe SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
The preliminary account helper is `0x140bfb3d0..0x140bfc586`; the main writer is
`0x140bfc590..0x140c045bd`; server-browser rendering is
`0x140b71430..0x140b71758`; autoexec synchronization is
`0x140bf9e40..0x140bfb185`. These are native addresses, end exclusive.
The complete sanitized reverse specifications were reviewed on 2026-10-13.
Private decompiler dumps and real account material are not included here.

Five generic constants are packaged in `fsm_panel.config_templates`. Each was
inspected for account-specific data before inclusion. `manifest.json` records
the exact LF/UTF-8 length, SHA-256, method, and literal/factory evidence. The
resource loader verifies every hash. Packaging must include the five `.template`
resources and `manifest.json` (for PyInstaller, collect the package's data).

| Resource | Bytes | Evidence |
|---|---:|---|
| sharedconfig.vdf.template | 1588 | factory `0x1405ec150`, literal `0x1405ec197` |
| remotecache.vdf.template | 8 | factory `0x1405eb8e0`, literal `0x1405eb927` |
| localconfig.vdf.template | 1592 | factory `0x1405ec500`, literal `0x1405ec547` |
| friends-block.vdf.template | 344 | factory `0x1405ec490`, literal `0x1405ec4d7` |
| serverbrowser_hist.vdf.template | 70 | renderer `0x140b71430` |

## Recovered contract represented by the plans

`build_account_config_plan` takes explicit settings, integer Steam ID, decoded
video/machine text, generic templates, injected filesystem facts, and time. It
performs no IO. The logical Steam directory uses literal backslash splitting of
the executable setting. The account ID is `str(steam_id - 76561197960265728)`;
there is no clamp or unsigned wrap. Integer-only inputs are a rebuild boundary.
The caller supplies the result of `qmmconnect_data.get(str(uid))` explicitly as
`mapped_localconfig`; the plan does not execute custom mapping or logging objects.

The returned `groups` describe the legacy order, handlers, and continuations.
They are descriptive data, not a legacy executor. Operations within each handler
scope stop on its first failure; the documented handler then controls continuation.
Predicated operations carry a `when` field. The `writes` collection describes
the successful branch selected by supplied facts, including sequential default
and mapped writes to the same localconfig path when both stages apply.

1. List userdata. A list failure logs `Check your STEAM_PATH!` and returns only
   from the preliminary helper; the main writer continues. Otherwise try account
   mkdir without `exist_ok`. Its bare handler includes the success log and logs
   the already-existing message when `force` is false. Log failures in that
   handler can propagate.
2. Read the panel's `settings\\cs2_video.txt`, then machine-convars template via
   `abspath` and legacy-default text opens. A source-read error propagates. The
   preliminary helper may already have created an account directory.
3. Remove `730/remote` as a directory or file, swallowing ordinary exceptions.
   In a separate shared handler, remove the two known user-convars filenames;
   failure on the first prevents checking the second.
4. Try remote7 mkdir. Then write sharedconfig, server-browser history and
   remotecache in one bare handler that prints a traceback and skips remaining
   writes after a failure. History renders `% str(round(now - 60))`, preserving
   Python ties-to-even rounding.
5. Try cfg mkdir and empty-open the two cfg outputs. The unusual
   `Settings folder was added!` message belongs to the exception path, when
   `force` is false. A `finally` copies video then machine, with ordinary-exception
   reporting, even if the earlier exception-path log raises. Such a pending
   exception propagates after the finally block.
6. Default localconfig stage runs when overlay is disabled **or** mapped value
   **is not None**, including empty string and false values. An existing file is
   opened as UTF-8 and closed without reading. The shipped friends-block method
   returns a fixed nonempty constant, so existing contents are preserved; this
   is not a substring test. Missing localconfig gets the full default template.
7. A distinct restore stage runs only when `not NO_AVAST_MODE` and mapped value
   **is not None**. It writes the exact mapped string using UTF-8, including an
   empty string. Its inner write errors print; mapping/print errors are swallowed
   by the outer ordinary-exception handler. NO_AVAST truth evaluation is outside
   that handler. The legacy non-string write can truncate before failing.

`force` changes human log output only. It does not gate writes, removals or mkdir.
The plan's `log_messages` assumes ordinary filesystem/logging success except the
provided missing/existing facts. Other stdout diagnostics remain separate ops.

`build_autoexec_plan` uses explicit `CONFIG_FILE_NAME` and decoded source/old
content. The global initializer is not guessed. The logical destination is
`ntpath.join(csgo_path, "game\\csgo\\cfg")`. Equal complete text returns True
before either removal, preserving both autoexec and the legacy-name file. On
mismatch the original reads/tries to remove autoexec, separately tries to remove
the named legacy file, writes/closes autoexec, and returns True. Read/removal
errors are swallowed; a final write/close failure reports and returns False with
no rollback. `existing_content=None` represents a missing/failed old read.
The helper never creates cfg. Identical names can produce two removal attempts.

## Safe work-copy API

```python
from fsm_panel.adapters.account_config import (
    AccountConfigSettings, create_config_workspace,
    preview_account_config, apply_account_config,
)
from fsm_panel.adapters.autoexec import preview_autoexec, apply_autoexec

workspace = create_config_workspace(r"D:\offline-fixtures\new-copy")
settings = AccountConfigSettings(
    steam_path=r"C:\logical-only\steam.exe", login="synthetic-account",
    force=False, disable_steam_overlay=True, no_avast_mode=True,
    mapped_localconfig=None,
)
preview = preview_account_config(
    workspace, settings, steam_id=76561197960265770,
    video_source=r"D:\offline-fixtures\inputs\video.txt",
    machine_source=r"D:\offline-fixtures\inputs\machine.vcfg",
    now=1800000000.5,
)
result = apply_account_config(preview)
```

Creation requires a nonexistent root with an existing parent; it does not adopt
or mark an existing directory. It creates an ownership marker and fixed `steam/`
and `game/` children. `open_config_workspace(root)` reopens an existing adapter
workspace with a valid marker. Configured logical paths never select filesystem
write locations. Callers can separately place synthetic or deliberately copied
offline data into these children. This adapter does not copy a live Steam tree.

Account writes target only `steam/userdata/<derived-id>`. Autoexec writes target
only `game/game/csgo/cfg`, which must already exist. Autoexec's `source_path` and
`config_file_name` are mandatory; the source path need not share its basename.
All source files remain read-only inputs outside the owned workspace. Sources
inside the workspace, identical account-source files, and hardlink aliases are
rejected. Destination paths and ancestors reject symlinks, junctions, reparse
points, special files, and file hardlinks. Panel.exe ancestors and Steam.exe
ancestors above the workspace are refused. Windows reserved names, absolute
filenames, traversal, alternate data streams and trailing-dot/space names fail
validation. The marker is an ownership convention, not an adversarial security
credential; it must never be hand-created on an original or live installation.

Previews retain source hashes/stat identities and a bounded snapshot of every
file and directory in the affected account/cfg tree, including untouched entries.
Current bounds are 16 MiB per file, 128 MiB and 10,000 entries per tree. Apply
reconstructs the preview and rejects stale or altered inputs before staging and
again before committing. The local lock serializes these two adapters' writers.

The writer reserves a unique stage within the workspace, copies the whole
affected tree without following links, verifies the copy, and applies changes
there. Only the known remote730 subtree permits recursive logical removal;
the two convars and explicit autoexec filenames must be files. Other contents
remain preserved. Canonical containment is checked before recursive copy,
removal and rename. Completed staged files are flushed and fsynced. The original
tree is renamed to a unique backup and the stage is renamed into its place.
An ordinary failure after the first rename attempts to restore the original;
it never overwrites a concurrently appearing target during rollback.

`ConfigApplyResult` reports the target and retained backup. `ConfigApplyError`
reports a stable code, `backup_path`, any remaining `staging_path`, and
`rollback_succeeded`. A failed rollback leaves the backup for recovery; it must
be inspected and moved back to the recorded target only after checking that the
target is absent and all paths remain inside the owned workspace. Cleanup
failures never replace this recovery information. No automatic backup deletion
occurs. A stale preview before a move raises `ConfigStalePreviewError`.

## Deliberate differences and limits

The safe writer is an all-or-nothing staged attempt for ordinary handled failures,
not the legacy sequence of destructive truncations and swallowed errors. Invalid
mapped types, unreadable snapshots, invalid encodings and missing autoexec cfg
fail before commit. A source-read failure does not create the preliminary account
directory. It reports exceptions rather than pretending that a legacy `None` or
boolean result described transaction success. It does not execute supplied logs.

The default new encoding policy is explicit UTF-8; callers can choose another
codec for legacy-default source reads/writes. Source reads use universal newlines;
generated writes use deterministic Windows LF-to-CRLF translation. Mapped restore
always uses UTF-8 as recovered. No locale-dependent encoding is inferred. Generic
template resources retain their original LF bytes. Existing preserved files and
no-op autoexec files retain exact bytes. Equal autoexec produces no stage, backup,
deletion, or write and preserves timestamps.

Two directory renames are not one atomic exchange. A brief missing-target interval
exists; process termination or power loss can leave a backup requiring recovery.
The writer does not promise crash-safe transactions, preservation of every NTFS
ACL/alternate stream, or protection from hostile concurrent filesystem mutations.
Checks minimize ordinary stale/copy races; external processes do not share the
adapter lock. These limitations reinforce the dedicated offline-copy boundary.
The wider call schedule, mapping population, GSI generation, and CONFIG_FILE_NAME
initialization remain outside this recovered slice.

## Desktop controls

`ConfigFilesWidget` provides separate account and autoexec tabs. First create a
new workspace or open one previously created by the adapter. Account preparation
requires an explicit JSON settings import containing `STEAM_PATH`,
`DISABLE_STEAM_OVERLAY` and `NO_AVAST_MODE`; missing keys are rejected rather than
filled with presumed legacy defaults. Enter Steam ID, login, the two source
templates, and the injected Unix timestamp. The mapped localconfig copy is
optional. UTF-8 and unchecked force are visible new-interface choices.

Settings and mapped-copy files use the public guarded `read_config_input` API:
they must stay outside the workspace, reject link aliases, use bounded reads and
universal newline decoding, and retain their stat/hash snapshots. Those snapshots
are attached to the account preview for another check after staging, immediately
before the directory replacement. Editing either file during staging therefore
invalidates the attempt without changing the target.

Preview displays the same fingerprint function used by the CLI, write/removal
counts, and localconfig action. It does not display raw config text, settings
secrets, or arbitrary exception strings. Every input edit invalidates stored
previews and clears displayed fingerprints. Equal autoexec content disables
apply and preserves both destination files. Successful apply reports the backup
path; recovery failures retain and show backup/staging paths and rollback status.

Filesystem work runs through `asyncio.to_thread`; queued Qt slots update the
controls. Closing waits for pending file operations rather than cancelling a
thread in the middle of a directory replacement. The standalone widget creates
and closes its own `AsyncBridge`. A host window can instead construct
`ConfigFilesWidget(shared_bridge)` and must `await widget.aclose()` before shutting
down that bridge. The widget never reads host-window settings implicitly.
