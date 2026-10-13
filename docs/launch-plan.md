# Offline Steam/CS2 launch preparation

`fsm_panel.adapters.launch_plan` produces immutable review data for the recovered
launch preparation core. It does not read the host environment, inspect paths,
generate randomness, consult a clock, write files, modify the registry, or launch
processes. No executor is provided.

The three entry points are `build_launch_plan`, `prepare_loginusers`, and
`userdata_id`. The first two are separate because login selection needs a Steam
ID and a timestamp. The recovered login-selection block runs for both values of
`NO_AVAST_MODE` once execution reaches it.

## Build a plan

All settings and facts are explicit. These synthetic values are an example,
not defaults recovered from the original.

```python
from fsm_panel.adapters.launch_plan import (
    LaunchFacts,
    LaunchSettings,
    build_launch_plan,
    prepare_loginusers,
    userdata_id,
)

settings = LaunchSettings(
    steam_path=r"C:\SyntheticSteam\steam.exe",
    steam_launch_options="-silent",
    additional_launch_options="",
    cs_width=640,
    cs_height=480,
    no_avast_mode=False,
    steam_launch_only=False,
    ancient_map_preload=False,
    running_2vs2=False,
    user_profile_new_link=r"C:\SyntheticProfiles",
    enable_shaders_caching=True,
)
facts = LaunchFacts(
    launch_dota2_exists=False,
    shader_link_exists=False,
    shader_link_is_symlink=False,
)
launch = build_launch_plan(
    settings,
    login="synthetic_user",
    update_mode=False,
    environment={"USERPROFILE": r"C:\SyntheticHost"},
    facts=facts,
)
selection = prepare_loginusers(
    settings.steam_path,
    steam_id=76561197960265729,
    login="synthetic_user",
    now=1000.5,
)
print(launch.status, launch.used_port, launch.environment)
# Starting 21201 None
print(selection.timestamp, userdata_id(76561197960265729))
# 820 1
```

Pass `ipc_random` in the inclusive range `1..9999` whenever `no_avast_mode` or
`update_mode` is true. The planner never generates it. Otherwise the value is
ignored, reflecting the branch that never invokes `random.randint`.

`steam_path=None` selects `settings.steam_path`; an empty override stays empty.
The override affects only the launch executable. Always pass the settings path
to `prepare_loginusers`, as shown above.

`LaunchSettings` names correspond directly to the recovered attributes:
`steam_launch_only` to `SteamLaunchOnly`, `running_2vs2` to `RUNNING2VS2`, and
the other fields to their uppercase spellings. Width and height correspond to
`CS_WIDTH` and `CS_HEIGHT`; no resolution parsing is inferred here.

## Argument and environment contract

`LaunchPlan.main` contains the exact prepared string. `argv` is a tuple containing
the executable followed by `main.split(" ")`. This preserves empty fields and
does not interpret quotes. It is not a parsed Windows command line.

The reconstructed transformations intentionally retain the original behavior:

- Base options use `rstrip()`, including trailing Unicode whitespace. Leading
  whitespace and interior tabs remain.
- Update mode prefixes `-login `. NO_AVAST or update mode adds
  ` -master_ipc_name_override ` followed by login and random integer with no
  separator between them.
- The game branch appends app 730, English, `autoexec.cfg`, dimensions, console
  logging, an optional `-allowmultiple`, the login log filename, and additional
  options. `-allowmultiple` depends only on NO_AVAST.
- The Dota sentinel globally replaces `730` with `570` in the string constructed
  so far. This affects options, dimensions and IPC text, but precedes the log
  filename and additional options.
- `-novid`, `-nojoy`, and optional map preload use substring membership. A longer
  option containing `-nojoy` already suppresses its addition. Map preload needs
  update mode and `ANCIENT_MAP_PRELOAD`; `RUNNING2VS2` selects `de_inferno`, otherwise
  `de_ancient`.
- Removal globally deletes the substrings ` -d3d9ex`, ` -disable_d3d9_hacks`, and
  ` -dxlevel 90`. This can remove prefixes of longer arguments. A spelling at
  the start of the string or after a tab does not match the required leading space.
- Steam-only mode globally removes `applaunch`, preserving its hyphen, app ID,
  surrounding spaces and any other text. It skips game additions and filtering.

The plan reports `status="Starting"`, `used_port=21201`, and
`creationflags=8` (Windows `DETACHED_PROCESS`). Its `shell=False` reports the
effective default: the original Popen call supplies no shell keyword.

The original copies the environment and assigns `VPROJECT="1"` in both branches.
It passes that copy only for NO_AVAST. Therefore `LaunchPlan.environment` is
`None` when NO_AVAST is false, including update mode. This means omit Popen's env
keyword and inherit the host environment; it does not mean pass an empty mapping
or assign VPROJECT in the current process.

For NO_AVAST, the returned environment is a copied, read-only mapping containing
the inherited values, `VPROJECT="1"`, and the profile overrides described below.
Mutating the input mapping after preparation cannot alter it.

## Profile and shader actions

Profile paths use literal backslash concatenation:
`user_profile_new_link + "\\" + login`. No normalization or name sanitization
occurs. `USERPROFILE` becomes that profile and `LOCALAPPDATA` becomes its
`\\AppData\\Local` child in the proposed environment.

`profile_directory` is a `MakeDirectories` action with `exist_ok=True`. It creates
that Local directory before the shader-specific try block. Its exception policy
is `propagate` out of this recovered local block; the enclosing launch method's
exception behavior is not claimed.

Despite its positive name, `enable_shaders_caching=False` enables the shader
block. `shader_cache` is absent when the flag is true or NO_AVAST is false.
Otherwise its ordered operations are:

1. Strictly look up the inherited `USERPROFILE` and append
   `\\AppData\\Local\\NVIDIA` for the target. An empty value is still present.
2. Plan `MakeDirectories` for the Windows dirname of the profile's NVIDIA link,
   with `exist_ok=True`. This second directory operation is deliberately retained.
3. If injected `shader_link_exists` OR `shader_link_is_symlink` is true, plan
   `RemoveFile`. This models `os.remove`, including a broken link. It is never
   recursive directory removal.
4. Plan `CreateDirectorySymlink(target, link_path, target_is_directory=True)`.

The group has `on_exception="report_and_continue"`: the first failed operation
skips the rest of the group, after which profile environment assignment continues.
An existing directory may make `os.remove` fail. The planner cannot predict
permissions, path races, directory types, or actual operation outcomes.

Missing inherited USERPROFILE is represented by
`shader_cache.caught_error="missing_userprofile"` and an empty operations tuple.
It is a strict lookup failure before any shader action. The initial directory
action and proposed profile environment remain. This models the local catch
without printing a traceback or exposing environment values.

## Login selection preparation

`prepare_loginusers` returns `LoginusersPlan.path`, `content`, `registry_command`,
`timestamp`, and operation metadata. The recovered order is a VDF overwrite,
successful close, then registry command, inside one try/except Exception block.
A failure before registry invocation skips that command and continues toward
Popen. A nonzero registry return code is not checked.

The path is calculated exactly as
`"\\".join(settings_steam_path.split("\\")[:-1]) + "\\config\\loginusers.vdf"`.
A bare filename, empty input, or forward-slash-only input therefore yields
`\\config\\loginusers.vdf`. It does not use the optional launch executable override.

The VDF contains one `users` account with AccountName, empty PersonaName,
RememberPassword `1`, WantsOfflineMode `0`, SkipOfflineModeWarning `0`,
AllowAutoLogin `1`, MostRecent `1`, and Timestamp in that order. Timestamp is
`str(round(now - 180))`, preserving Python's ties-to-even rounding.

Content preserves the recovered leading newline, indentation, and final twenty
spaces. It is pre-write LF text. The original opens mode `w`, encoding `utf-8`,
with normal Windows text-mode newline translation. Existing content is overwritten;
there is no parsing, merging, escaping, backup, or directory creation in this block.

The registry string preserves the recovered `reg add` construction for
`HKCU\\Software\\Valve\\Steam`, value `AutoLoginUser`, type `REG_SZ`, with an
unquoted login after `/d` and a final `/f`. Metadata records `shell=True`, captured
stdout/stderr, `text=True`, and no `check=True`. The value is review data and is
never passed to a shell by this module. Quotes, newlines, percent signs and shell
metacharacters in login remain unescaped. Any future executor needs a separately
specified contract; this planner is not its authorization token.

The original opens/truncates the VDF before evaluating the template. This pure
implementation evaluates the complete proposal in memory and cannot reproduce
that filesystem failure side effect. No generated config or registry action is
marked applied.

## Userdata identifiers and validation boundary

`userdata_id(steam_id)` returns `steam_id - 76561197960265728`. For ordinary Python
integers it preserves unbounded signed arithmetic: the base yields zero, smaller
values yield negatives, and larger values are not reduced to 32 bits. The video
caller separately stringifies this result.

The original helper performs subtraction without calling `int()`. Its general
numeric dispatch can support non-exact-int operands. The rebuild accepts exact
Python integers only, rejecting booleans, strings and custom numeric subclasses.
This explicit type validation is a new boundary, not recovered legacy behavior.

The planner similarly accepts exact strings and booleans, dimensions as exact
strings or integers, and environment keys/values as exact strings. It does not
validate paths, dimensions, account-name spelling, unsigned ranges, command
safety, or Steam acceptance. Loginusers accepts an exact integer Steam ID and a
finite exact integer/float timestamp. These are reconstruction input rules.

Validation raises `LaunchPlanError` with a content-free `.code`:
`launch_string_required`, `launch_boolean_required`, `launch_integer_required`,
`launch_dimension_type`, `launch_environment_strings_required`,
`launch_ipc_random_range`, or `launch_finite_time_required`.

Dataclasses are frozen; generated collections are tuples or an immutable copied
environment mapping. Their representations omit settings text, argv, commands,
content, paths, and inherited environment data. No function logs input values.
Intentional access to explicit fields still reveals them. Keep those fields out
of logs and generic telemetry. For explicit serialization, use
`dict(plan.environment)` when it is present; `dataclasses.asdict` cannot deepcopy
the read-only mapping wrapper.

## Evidence and remaining scope

Evidence is bounded static instruction/data-flow analysis of target SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
The root is `RvorgLqLUOoP`, source descriptor `FSMPANEL_CS2.py:9341`, native range
`[0x14067ae90, 0x14068b76b)`. This is a reconstruction, not recovered source.

| Behavior | Native addresses |
|---|---|
| Starting status and port | `0x14067d349..0x14067d399` |
| Base options, update prefix, IPC suffix | `0x14067d3c4..0x14067d7bb` |
| Steam-only predicate | `0x14067d7fe..0x14067d87a` |
| Game string and Dota replacement | `0x14067da07..0x14067de03` |
| Allowmultiple, logfile, additional options | `0x14067de49..0x14067e117` |
| Added parameters and banned substrings | `0x14067e109..0x14067e782` |
| Steam-only replacement, split and path fallback | `0x14067ea57..0x14067ec83` |
| Environment copy and profile setup | `0x14067ee4b..0x14067f230` |
| Inverted shader branch, operations and local catch | `0x14067f2bb..0x14067fb14` |
| Profile environment assignments | `0x14067fb86..0x14067fbe4` |
| Loginusers/registry shared preparation block | `0x14067fc23..0x140680852` |
| Launch argv, flags and env branch | `0x140680892..0x140680ccb` |
| Userdata helper and exact-int subtraction | `0x140bfb190..0x140bfb3ce`, `0x142d0d7b0..0x142d0d8c6` |
| Generic subtraction fallback | `0x142d17f20..0x142d17ff7` |

Unimplemented surrounding behavior includes the preceding config helper
(`0x140bfc590..0x140c045bd`), actual config/GSI writers, layout helper, entry gating,
launch-locker timing, process/window detection, updater validation, login entry,
retries, and global exception handling. A nearby missing-autoexec log does not
prove creation: `0x14067cd39..0x14067d349` checks existence and logs without a
bounded write. No filesystem, registry or process execution adapter is supplied.

Tests use synthetic inputs and expected strings, branch matrices, arbitrary
integer arithmetic, immutable snapshots, and forbidden host dependencies. They
validate this local implementation against the static contract. They do not
establish original runtime parity, Steam compatibility, Windows privilege effects,
or complete launch behavior. Private dumps and original files are not distributed.
