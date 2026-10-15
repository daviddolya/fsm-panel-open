# Offline GSI configuration preparation

Use this adapter to prepare a Game State Integration (GSI) file in an explicitly
created offline workspace. It writes one indexed cfg file and retains the
previous cfg tree as a backup. It never starts a listener, probes a port, launches
Steam or CS2, or modifies the original distribution.

## Prepare and apply a preview

The example creates disposable synthetic data and a fresh token. The selected
port and file index are independent, explicit inputs.

```python
import secrets
import tempfile
from pathlib import Path

from fsm_panel.adapters.account_config import create_config_workspace
from fsm_panel.adapters.gsi_config import apply_gsi_config, preview_gsi_config

parent = Path(tempfile.mkdtemp(prefix="fsm-gsi-example-"))
workspace = create_config_workspace(parent / "offline-copy")
# The writer requires an existing cfg directory.
cfg = workspace.game_root / "game/csgo/cfg"
cfg.mkdir(parents=True)

# Keep the source outside the destination workspace.
token_source = parent / "fresh-token.txt"
token_source.write_text(secrets.token_urlsafe(32), encoding="ascii")
preview = preview_gsi_config(
    workspace,
    running_on_port=6969,
    gsi_file_index=1,
    token_source=token_source,
    encoding="utf-8",
)
result = apply_gsi_config(preview)
assert result.changed
assert result.target_path == cfg / "gamestate_integration_1.cfg"
assert result.backup_path is not None
```

The output URI is `http://127.0.0.1:6969`. Preparing a file does not establish that
the port is available, that a server is listening, or that CS2 will use the file.
The source token, generated cfg, and backups must remain private. Preview
representations hide token content and its source path; `preview.plan.content`
and `preview.transaction` still hold sensitive data in memory.

## Public API

| Function | Result and boundary |
| --- | --- |
| `load_gsi_template()` | Returns the packaged sanitized string after a fixed length and SHA-256 check. |
| `render_gsi_config(running_on_port, token)` | Renders the fixed template with the supplied port and token. Reads only the packaged template resource. |
| `build_legacy_gsi_plan(*, csgo_path, running_on_port, gsi_file_index, template)` | Returns a descriptive `GsiConfigPlan` using explicit strings and inert scalar values. Performs no IO. |
| `select_legacy_gsi_port(probe_outcomes)` | Returns `LegacyGsiSelection` from supplied booleans. Performs no probes or allocation. |
| `preview_gsi_config(workspace, *, running_on_port, gsi_file_index, token_source, encoding="utf-8")` | Reads and validates the inputs and complete cfg tree without writing. Returns `GsiConfigPreview`. |
| `apply_gsi_config(preview)` | Revalidates and commits through the shared config transaction. Returns `ConfigApplyResult`. |

`running_on_port` must be an exact integer from 1 through 65535. The safe file
index must be an exact integer from 1 through 10; booleans are rejected. The
index bound is a **new safeguard**, based on the recovered selector's ten
candidate ports. The original writer did not validate the index or port.

Tokens must contain 32 through 256 ASCII characters from `A-Z`, `a-z`, `0-9`,
`-`, and `_`. This is stricter than the existing receiver's printable-ASCII
contract. Quotes, slashes, backslashes, whitespace, controls, and other
punctuation are rejected. Token files may end with exactly one LF or CRLF;
additional whitespace is not stripped. The renderer itself accepts no line
ending. Format checks do not establish randomness; generate fresh values with
`secrets.token_urlsafe(32)`.

The selected encoding decodes the token source and encodes the output. UTF-8
is the default. Output line endings are deterministic CRLF. This encoding policy
is a reconstruction choice, not a claim about the legacy runtime's default codec.

## Desktop panel

Create a workspace on **Конфиги игры**, then select that existing workspace in
the GSI panel. Choose a token file outside the workspace, enter the destination
port and file index, and select an encoding. The default encoding is UTF-8.
The panel neither generates tokens nor provides a field for pasting token values.

**Предпросмотр** opens and validates the marked workspace in the background.
The summary shows the indexed filename, write/removal counts, and plan
fingerprint. It never displays the rendered configuration or token contents.
Review that summary, then select **Применить к копии**.

Changing any input clears the reviewed preview, including a temporary edit that
is changed back. Applying checks the reviewed fingerprint and revalidates source
and destination snapshots. A stale preview must be regenerated. Successful
writes display the backup path; failures show fixed error codes and available
recovery paths without arbitrary exception text.

`GsiConfigPanel(owner)` uses `owner.bridge` for all file operations. The owner
must await `panel.aclose()` before shutting down that shared bridge. Closing
blocks new requests and waits for pending file work without cancelling a tree
replacement. Qt remains responsive while the background work finishes.

## Recovered behavior and evidence

The static source is `FSM_PANEL v.3.1.2/Panel.exe`, SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
The sanitized writer specification and sanitized template export were reviewed
without reading the private authentication literal.

| Evidence | Address |
| --- | --- |
| Launch function containing the writer, `JclxWVpqCTxI` | `[0x140976d80, 0x140984d79)` |
| Config-directory suffix/join | `0x14097c96b`, `0x14097c99c` |
| GSI writer slice | `0x14097da31..0x14097e519` |
| Template constant 5513, slot/load | `0x142deb3e8`, `0x14097da3f` |
| Ordinary `str.replace` call and cached implementation | `0x14097dcf1`, `0x142d0b760` |
| Port selector/server helper, `OqgCxbeJWAsr` | `[0x140a024c0, 0x140a041aa)` |

The legacy plan represents these operations:

1. Replace every `PORT_PLACEHOLDER` with `str(running_on_port)`. Rendering is
   outside the local write handler. It does not replace an authentication token.
2. Format `gsi_file_index` with an empty format specifier and construct
   `gamestate_integration_<index>.cfg`.
3. Join that filename beneath `ntpath.join(csgo_path, r"game\csgo\cfg")`.
   Open in text mode `w` without an explicit encoding and write the content.
4. Catch ordinary `Exception` from filename formatting, final join, context
   management, or writing. Log `Wrong CS2 path!` and continue the outer launch
   sequence if logging succeeds. A logging failure can propagate.

The block rewrites equal content, creates no directory, and deletes no older
indexed file. The plan records operation order and exception scopes; it is
descriptive data, not a legacy executor.

The pure plan accepts exact `str`, `int`, finite `float`, `bool`, or `None` for
the port and index. Exact strings are required for path and template. These
inert-input checks are new boundaries that prevent custom object methods from
executing. They intentionally do not add port or path validation to the
descriptive legacy transformation.

The supplied-outcome selector consumes a list or tuple of booleans. `True` means
the original GET/status formatting/print expression completed; `False` means an
ordinary exception occurred within that expression. It chooses the first false
outcome among ports 6969 through 6978 and calculates `index = port - 6969 + 1`.
If all ten outcomes are true, it retains port 6969 and index 1. Unconsumed values
are ignored; a missing or non-boolean consumed outcome is rejected.

No HTTP status code is interpreted as a success or failure by this selector.
The recovered helper has no request timeout or bind test. It assigns the port
and index before server construction succeeds. Its subsequent retry loop uses
the same selected port after a 0.5-second sleep; this adapter does not implement
that server loop.

## Sanitized resource

`fsm_panel.gsi_templates/gsi.sanitized.template` contains the reviewed template
with the original authentication value replaced by `TOKEN_PLACEHOLDER`. The
legacy writer never substituted this marker. Substituting a fresh caller token
is an intentional reconstruction change.

The resource has 1268 UTF-8 bytes, no final newline, and SHA-256
`6b02e3f2bdc3afef44437795cb36ea5c1096e1ec980b91f3d55ab2e40f84ed8f`.
Its manifest records the source hash and instruction anchors. Packaging must
include the `.template` and `manifest.json` resources.

All recovered non-auth fields, including indentation and data-field selection,
are retained. The root is `Observer Players`; timeout is `1.0`, buffer and
throttle are `0`, and heartbeat is `20.0`. All three output precision fields are
`3`. The data keys are provider, map, round, player_id, player_state,
allplayers_id, allplayers_state, allgrenades, allplayers_match_stats,
allplayers_position, allplayers_weapons, bomb, map_round_wins, and
phase_countdowns, each with value `1`.

## Workspace, transactions, and failures

The only writable cfg directory is
`<workspace>/game/game/csgo/cfg`. The `ConfigWorkspace` marker must be valid,
and cfg must already exist. Source files inside the workspace, hardlinks,
symbolic links, junctions, and reparse points are rejected through the existing
account-config safety boundary. Original-distribution and live-Steam ancestor
checks also apply. See [account config preparation](account-config.md).

Preview captures token-file identity, raw-byte hash, and the entire cfg tree.
Apply holds the same lock as account-config and autoexec writers. It validates
the preview, stages the tree, writes the selected file, validates again, retains
the old tree as a backup, and installs the staged tree.

Every successful apply reports `changed=True`, including identical output.
Other indexed GSI files, autoexec, nested files, and unrelated cfg contents are
preserved. A change to any source or cfg-tree member makes the preview stale,
even when the token's decoded value is unchanged.

| Error | Meaning and next step |
| --- | --- |
| `gsi_port_invalid` / `gsi_index_invalid` | Supply integers within the documented bounds. |
| `gsi_token_invalid` | Supply a fresh URL-safe token with the required length. |
| `gsi_config_directory_missing` | Prepare the offline cfg directory before previewing. |
| `gsi_template_unavailable` / `gsi_template_hash_mismatch` | Restore the reviewed packaged resource. |
| `config_source_destination_overlap` | Move the explicit token source outside the offline workspace. |
| `config_preview_stale` | Inspect changed files, then create a fresh preview. No staged write is committed. |
| `config_apply_failed` | The transaction failed and rollback succeeded; the previous tree remains available. |
| `config_rollback_failed` | Inspect `ConfigApplyError.backup_path` and `staging_path`; preserve them for manual recovery. |

Read and permission failures may raise `OSError` before staging. The shared
transaction restores the original tree after replacement failures when possible.
It never clobbers an independently created replacement to force rollback.
Backups contain earlier tokens when the previous cfg did; do not publish them.

## Verification limits

The behavioral tests use real offline file mutations and injected write failures.
They check rendered fields, unconditional rewrites, preservation of other files,
backup recovery, links, stale previews, and the final source check after staging.
They do not execute the original application or establish live game parity.

Still unverified: all GUI conditions that reach this writer, its ordering relative
to listener startup, and the original handler's parsing/authentication behavior.
The reconstruction's observation receiver remains a separately tested component;
this file adapter makes no claim that the receiver matches the original handler.
