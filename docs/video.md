# Offline video-template preparation

`fsm_panel.adapters.video` prepares an explicitly selected writable template copy
from an explicitly selected Steam CS2 video config. The source config is opened
read-only. It does not locate Steam, select an account, launch a process, install
the template into Steam, or wait five seconds. Caller scheduling remains separate.
No proprietary template is included.

## Operator entry points

The desktop's **Видео-конфиг** tab selects a writable template copy and a read-only
Steam config. Preview shows the resulting text and actual GPU identifiers. Apply
is enabled only for a changed preview and becomes unavailable when inputs change.
File work runs off the Qt thread; shutdown waits for an in-progress write.
This is an offline preparation action available without a license or game launch.

From source, the same operations are available without Qt:

```powershell
uv run python -m fsm_panel.maintenance video --template C:\work\cs2_video.txt --steam-config C:\source\cs2_video.txt
uv run python -m fsm_panel.maintenance video --template C:\work\cs2_video.txt --steam-config C:\source\cs2_video.txt --apply
```

Commands emit JSON. Add `--policy legacy` only to request the recovered global
replacement. Optional `--expect-template-sha256` and `--expect-source-sha256`
bind a later apply command to hashes returned by a previous preview. An error
returns exit code 2; failed apply also reports its retained backup, when available.

```python
from fsm_panel.adapters.video import preview_video_template, apply_video_template

preview = preview_video_template(
    "working-copy/cs2_video.txt",
    "selected-source/cs2_video.txt",
    policy="fields",
    protected_roots=("original-distribution",),
)
# Show before_*, source_*, after_*, changed and rendered_content for review.
result = apply_video_template(preview)
print(result.changed, result.backup_path)
```

An unchanged template prints `False None`. A changed template prints `True` and
the path to its newly created backup. The original distribution should remain
outside the chosen working-copy directory.

## Replacement policies and evidence boundary

The bounded static analysis of the original helper established four decimal-string
captures using `"VendorID"\s+"(\d+)"` and `"DeviceID"\s+"(\d+)"`, followed by an OR
comparison. Either unequal ID triggers both replacements. String comparison keeps
leading zeros significant. Equal captures skip the write. The original writes the
panel template, not the Steam source config.

`legacy` reproduces that local transformation: first globally replace the template
vendor string with the source vendor string, then globally replace the template
device string with the source device string. Replacements affect unrelated text
and can cascade. For template vendor `1` / device `2` and source vendor `2` /
device `3`, the rendered template has vendor `3` / device `3`. The frozen preview's
`source_vendor_id` / `source_device_id` contain requested source values, while
`after_vendor_id` / `after_device_id` contain actual rendered values. The first
matching regex capture wins in legacy mode, including when keys are repeated.
Python regex decimal digits are supported without conversion to integers.

`fields`, the default, is a deliberate safety improvement: replace only the two
captured ID value spans, using their original offsets so substitutions cannot
cascade. It also rejects multiple occurrences of either quoted field name in
either file. This is a narrow field edit, not a full KeyValues parser: quoted
names in comments or unrelated strings count toward that conservative ambiguity
check. Inputs must contain both numeric quoted fields. Neither mode supplies a
default for missing or malformed captures.

Only the local branch and string transformation are characterized as recovered
behavior. Bounded decoding, safer path handling, backup/recovery and atomic writes
are new design decisions. Tests use synthetic configs and injected I/O failures;
they do not establish original runtime parity or live Steam compatibility.

## Preview and apply contract

`VideoPreview` is frozen. Its public fields include absolute `template_path` and
`steam_config_path`, `policy`, `before_vendor_id` / `before_device_id`, `source_*`,
`after_*`, `changed`, `rendered_content`, `template_sha256`, `steam_config_sha256`,
`encoding`, and `protected_roots`. Private snapshots also retain the original
template bytes and file identity, size, modification time and mode. Config content
is omitted from its representation. The object is intended for in-process use,
not persistence or transport as an authorization token.

Apply reconstructs and compares the complete preview before creating files, and
again immediately before replacing the template. Changes to either file, file
identity, policy, rendered content, or other preview fields invalidate the
proposal. Obtain a fresh preview after any successful changed apply. Even an
unchanged preview undergoes validation; a valid unchanged apply makes no writes
and creates no backup.

A changed apply stages the rendered bytes in the template directory and flushes
them with `fsync`. Before replacement, it writes and flushes a unique adjacent
`.bak` containing the exact previous bytes. `os.replace` atomically replaces the
template path. Existing backups are never overwritten. The result is a frozen
`VideoApplyResult(changed, template_path, backup_path)`; `backup_path` is `None`
for a no-op.

If staging or replacement fails, the original stays intact where possible.
An uncertain replacement failure is checked, and if the template contains this
operation's rendered bytes, an atomic restoration of the original is attempted.
A concurrent third value is never overwritten by rollback. A fully written
backup is retained on I/O failure, including when automatic restoration fails.
Incomplete staging files are removed during ordinary exception handling. If the
OS refuses removal, a temporary file may remain; that secondary failure cannot
hide the primary error, backup path, or rollback result.

The process-local lock serializes this module's applies. It does not prevent
external writes in the final verification/replacement interval, and this is not
a cross-process transaction or a power-loss durability guarantee. Callers should
operate on a private working copy with other writers stopped. File mode is
preserved; owner, ACL, alternate-stream and extended-attribute replication is not
implemented.

## Input and path safeguards

- Each file and the rendered output is limited to 1 MiB; an ID capture is limited
  to 128 characters. Legacy replacement growth is checked before allocation.
- UTF-8 with or without BOM and UTF-16 LE/BE with BOM are supported. Template BOM,
  encoding, whitespace and line-ending bytes are preserved except for replaced
  digits. Invalid decoding, UTF-32 and NUL-containing content are rejected.
  No locale-encoding guess is made; these encoding rules differ from the original
  platform-default text opens.
- Both paths must be regular files. Symlinks and Windows junctions in either path
  or their ancestors are rejected. A template with multiple hardlinks, a read-only
  template, and a template/source alias are rejected. These checks repeat at apply.
- A template inside any caller-supplied `protected_roots` is rejected. A template
  beneath an ancestor containing `Panel.exe` is also rejected to protect the
  original distribution. Pass known original roots explicitly, including renamed
  or unpacked distributions that lack that marker. No recursive distribution scan
  is performed. These checks do not claim protection against hostile concurrent
  filesystem namespace manipulation.

Errors expose a stable, content-free `.code`. Validation uses `VideoTemplateError`;
staleness uses `VideoStalePreviewError` (`video_preview_stale`). I/O apply failures
use `VideoApplyError`, with `.backup_path` and `.rollback_succeeded`:
`video_apply_failed` means the write failed with the original preserved/restored;
`video_rollback_failed` means automatic restoration was not confirmed. Retain the
backup for recovery and inspect the current working copy before another attempt.
