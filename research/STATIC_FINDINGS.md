# Static findings for FSM Panel 3.1.2

## Evidence and coverage

The exact 60,669,440-byte `Panel.exe` has SHA-256 `3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`. It is a native x64 PE carrying a Nuitka-compiled CPython 3.11 application. The executable was read only; it was never launched or patched. No live Steam account or external service was contacted by the analysis.

Three evidence classes must remain separate:

* **Validated static data/instructions:** PE parsing, Capstone instruction decoding, a constants decoder independently checked against the stored CRC, a full typed constant walk, and inert CPython marshal reads. These are ordinary local analysis, not REA Evidence.
* **Manual Ghidra:** Ghidra 12.1.4 headless with `-noanalysis`, bounded function disassembly and 30-second decompiler limits. Loader, video-configuration, signed-proof validator, and two GSI-handler pseudocodes completed. Decoder pseudocode contains an unresolved jump table; launch pseudocode timed out. A reusable project was saved privately.
* **REA JavaScript:** Two extracted embedded scripts parsed successfully through REA, producing 2,005 AST nodes and no parse failures. Application parse coverage is complete for those two inputs; 455 semantic unknowns remain. Parent Evidence `ev_599df6450cc5cd6c72ad0efaa93a69c49f1558ba19c4b9ead7a6dd5f7e5d0b62`, view Evidence `ev_8405e8b812cb7254ff52045854e6323bc0626ec2a024a42b510995c11659d519`. These do not establish native application coverage.

REA native provider availability passed with local Ghidra and a full JDK 21. Native auto-analysis failed after 1,193,534 ms with `OutOfMemoryError: Java heap space` in `ConstantPropagationAnalyzer`, before the bridge became ready, with a 2 GiB heap. A queued second import was stopped. No successful native REA decompilation is claimed. The task-owned REA/JVM processes were closed; default auto-analysis was not retried further.

## Recovered structure

The PE entrypoint is `0x142d22f30`; the unwind table contains 25,077 entries. RCDATA resource 3 occupies file offset 48,224,628, length 12,437,413. Its SHA-256 is `4905dcbc922bf9f5afe1d3aab5479bc12c483b80eec5095f8fcb7a43b276cb56`.

The constants loader at `0x142d1f210` obtains RCDATA 3, calls the decoder at `0x142cddb10`, verifies CRC32, then scans module-name/length records. The first eight bytes encode CRC32 `a2faa7c6` and payload size 12,437,389. The transformation removes a 16-byte seed prefix and uses a 256-byte substitution table at RVA `0x2dafbb0` plus an eight-way state update. The raw payload's CRC mismatch is expected before this transform. The decoded output is 12,437,397 bytes with SHA-256 `475cedef8d71ee0d9b040afc2d8d0eb453e7dcdf64e855419881d5f97fb38ced`.

All 458 module sections pass their declared constant counts, terminal `.` marker, and exact end offset. The grammar matches the archived Nuitka 2.3.11 format; this establishes format compatibility, not the exact compiler version. A second C-string substitution helper is at `0x142cddbf0`, using RVA `0x2dafcb0`.

| Module | Constant count | Payload bytes | Interpretation |
| --- | ---: | ---: | --- |
| `__main__` | 9,310 | 1,431,002 | Main compiled application |
| `__parents_main__` | 9,304 | 1,430,857 | Closely related packaged main module; exact runtime use unresolved |
| `sda_window` | 543 | 10,335 | Application-specific auxiliary UI candidate |
| `.bytecode` | 388 | 6,668,273 | Inert marshalled library code |

All 388 marshalled records were parsed with CPython 3.11 without execution, producing metadata for 12,593 nested code objects. Main application code is native; the bytecode section does not restore its Python source.

The main constant array is `0x142de07a0`. Its one-time wrappers are `0x140605140` and `0x14061af00`. A straight-line initializer at `0x140605180` creates 1,024 code-object descriptors through `0x142d1fd50`, preserving the source filename `FSMPANEL_CS2.py`, line numbers 1–37076, function names, parameter/local names, and argument counts. Most method names are obfuscated. The `App` class descriptor is at source line 13885, and its initializer is associated with native root `0x140733da0`. Root `0x140c092d0` references the `App` descriptor. The startup candidate `fWgmwJgPhnjI` is native root `0x140bd7350`, source line 36962, and references `App`, account-manager objects, thread creation, GSI/client state, and `mainloop`. A complete startup call graph has not been recovered.

After the `mainloop` region, the startup candidate constructs a hidden PowerShell command that waits for a process to exit, appends a random count of bytes to a file, changes its timestamp, and renames it. The surrounding locals refer to executable file paths and mutation commands. This is an observed original command-construction side effect; the exact trigger and target selection are unresolved. It was never executed during analysis and is not required merely to reconstruct legitimate account-management features.

Shipped modules include CustomTkinter/Tkinter, Pillow, pyautogui, pygetwindow, pywinauto, psutil, requests, steampy, and Windows automation support. Module presence alone does not prove every dependency is exercised.

## Launch contract recovered so far

The strongest launch candidate is native root `0x14067ae90`, source line 9341, compiled name `RvorgLqLUOoP`, four parameters: `self`, `PsrWppciGZgI`, `STEAM_PATH`, `UPDATE_MODE`. Its native body spans 67,803 bytes over two chained unwind ranges. All selected constant loads align with a complete linear Capstone decode from unwind range starts. This verifies that references belong to instructions, but not every branch predicate or runtime value.

The following operation order follows increasing native addresses and related local names. Exact branch conditions and failure transitions remain partly unresolved.

| Native location | Static observation | Contract implication / limit |
| --- | --- | --- |
| `0x14067b173` | `C:\Windows\Temp\locker.txt`, `last_acc_time`, file read/write, sleep, time values | Cross-launch timing/lock state exists. Exact lock protocol and race behavior need tracing. |
| `0x14067b1cf` | `STEAM_PATH`, `.exe`, explicit wrong-path message | Steam executable path is an input; full validation rules remain unknown. |
| `0x14067ce1a` | `CSGO_PATH`, `game\csgo\cfg`, `autoexec.cfg`, creation message | Launch prepares or ensures a CS2 autoexec file. Recovered snippets alone do not establish its complete contents. |
| `0x14067d388` | Integer 21201 followed by `USED_PORT` | Port state is initialized/referenced here. Allocation, collision handling, and relation to every listener are unresolved. |
| `0x14067d3fd` | `STEAM_LAUNCH_OPTIONS`, `-login `, `NO_AVAST_MODE`, ` -master_ipc_name_override `, random range 1–9999 | Command construction supports account login and an IPC-name override. Secret argument values were not exported. |
| `0x14067d7fe` | `SteamLaunchOnly`, GSI file index, normal app-launch fragment | There is a Steam-only mode and a Steam-plus-game mode. UI text independently describes both. |
| `0x14067da86` | ` -applaunch 730 -language english +exec autoexec.cfg -w `, `CS_WIDTH`, ` -h `, `CS_HEIGHT`, console/debug fragments | These are literal components of the CS2 launch command; not a fully reconstructed final command. |
| `0x14067dcf2` | `launch_dota2.txt` and pair `730`, `570` passed near string replacement | A file-controlled alternate app-ID path is a strong candidate; exact trigger predicate needs confirmation. |
| `0x14067decf` | `-allowmultiple `, `-con_logfile `, `.log `, `ADDITIONAL_LAUNCH_OPTIONS` | Multi-instance/logging/custom launch options are supported. |
| `0x14067e1a7` | `ANCIENT_MAP_PRELOAD`, `RUNNING2VS2`, `+map de_ancient` | Ancient map preloading is conditional. Nearby `-novid`, `-nojoy`, `-d3d9ex`, `-disable_d3d9_hacks`, `-dxlevel 90` participate in option processing; inclusion/exclusion rules are not fully proven. |
| `0x14067ee96` | `environ`, `copy`, `VPROJECT`, `USER_PROFILE_NEW_LINK`, directory creation | A child environment is constructed; exact assignments require further data-flow tracing. |
| `0x14067f2bb` | `ENABLE_SHADERS_CACHING`, `USERPROFILE`, `AppData\Local\NVIDIA`, `islink`, `remove`, `symlink`, `target_is_directory=True`, `LOCALAPPDATA` | Profile redirection and optional shader-cache directory linking are part of launch preparation. This is an original side effect, not an instruction to perform it during analysis. |
| `0x14067fc23` | `reg add "HKCU\Software\Valve\Steam" /v AutoLoginUser /t REG_SZ /d ` and `/f` | Original code constructs a Steam auto-login registry update. No registry change was made by this analysis. |
| `0x14067fdf4` | `\config\loginusers.vdf`, formatted template, file-writing calls | Original code prepares Steam login-user state. Template values and credentials are withheld. |
| `0x140680436` | `subprocess.run`, `PIPE`, keyword tuple `shell,stdout,stderr,text` | A shell command is invoked in this region; registry command is the nearby candidate. Full argument-flow proof remains open. |
| `0x14068096a` | `subprocess.Popen`, `DETACHED_PROCESS`, keyword tuples `creationflags,env` and `creationflags` | Child-process launch has alternate environment and default-environment paths. No child target was launched. |
| `0x14068120e` | `ACCOUNT_LOGIN_TIMEOUT`, sign-in window names in English/Russian, Win32 PID/window APIs | Launch waits for and automates Steam UI. Candidate helper `UCbDORCMUJWy` maps to `0x140666aa0`, line 8982. |
| `0x140682c09` | `steam_launched`, `steam_launched_final`, `retry_btn_clicks`, `launched_accounts.json`, thread creation | Runtime state and launched-account persistence are updated; exact persistence schema is unresolved. |
| `0x1406835e5` | `\logs\content_log.txt`, 3.0, 65536, regex timestamp/AppID730, `Verifying` | The workflow inspects the tail of Steam's content log to detect file verification. Timing comparisons and complete timeout behavior need confirmation. |
| `0x14068654f` | CS2 PID state, `pid_exists`, missing-process restart message, `FindWindow(...,"Counter-Strike 2")`, `SetWindowText` | It tracks the game process, can restart a missing process, and renames a CS2 window. Exact restart limits are unresolved. |
| `0x140688efc` | `hSjuJlBgrRKC` plus thread creation | Calls the video-configuration candidate after launch. |
| `0x14068937e` | `sessions\`, `.pkl`, `pickle.load`, cookies update | Original workflow loads a local saved session and applies cookies. These files were not deserialized during this analysis. |

The video helper `0x1406787d0`, source line 9317, has completed manual Ghidra pseudocode. It references a five-second sleep, `settings\cs2_video.txt`, Steam `userdata\...\730\local\cfg\cs2_video.txt`, regex extraction of `VendorID` and `DeviceID`, replacement operations, and writing the resulting configuration. The message “Fixed DeviceID and VendorID values in the config.” is a corroborating constant. Complete replacement direction should be checked before implementing destructive writes.

## Lobby, batch, and UI catalogue

`feature-catalogue.json` records exact native roots, code-object slots, source lines, parameter/local names, and config references. Its `validation` record gives the exact constant-reference and native-range counts for 22 selected native roots; every selected constant load aligns with a linear instruction decode from its unwind range start.

| Candidate | Native root / source line | Evidence |
| --- | --- | --- |
| Configuration initialization | `0x1406b0d40` / 10541 | Steam/CS2 paths, launch delay/login timeout, farming/lobby modes, co-op, batch interval, drop collection/send keys |
| Lobby composition | `0x1409d1f30` / 26215 | `LOBBY_INVITE_MODE`; locals include levels, lobby lists, friends lists, and main account |
| Lobby invitations | `0x1409fa930` / 26882 | `LOBBY_INVITE_MODE`; locals include codes, launched windows, and nicknames |
| `accept_invite` | `0x1409f6790` / 26817 | Explicit method name and pixel/top-bar/window geometry locals |
| GSI processing | `0x1409ffad0` / 26986 and `0x1409ff130` / 26970 | Explicit `do_POST(self)` and `get_info_about_players(self,payload)` metadata |
| Batch timer | `0x140b76f20` / 34716 | `CHANGE_BATCH_AFTER_N_MINUTES`; selected/launched-account locals |
| Farm/batch orchestration | `0x140b81ec0` / 34878 | Batch interval, co-op, drop collection/send, lobby shuffle-attempt keys; locals for game search, in-game counts, drops, armory passes, reconnects, and stuck accounts |

Curated UI/log text distinguishes invitation by codes or friends list, Steam-only launch, normal CS2 launch, AvastSandbox launch mode, armory pass mode, drop collection, timer-based lobby start, batch changes, and stopping farm orchestration. These are shipped text observations. Successful behavior for these modes is untested.

The two GSI handlers now have completed manual Ghidra pseudocode. `do_POST` reads the integer `Content-Length`, reads that many bytes from `rfile`, decodes UTF-8, parses JSON, calls `get_info_about_players(payload)`, and sends status 200 with `Content-Type: text/html` and an empty header-terminated response. The payload helper obtains `payload['provider']['steamid']`, converts it to an integer for membership in `LAUNCHED_ACCOUNTS_IDS`, and updates `GAME_INFO` with the original steamid value as key and the entire payload as value. Non-member IDs take the normal return path. The helper's exception path references an error-dump helper and `payload.json`. Bind address, listener authentication, HTTP size bounds, and listener lifetime are not established by these two functions.

## Embedded Node.js contracts

The activity script has SHA-256 `3349e3c1232551beb4f90f1849c39f289c84e014df0e0cbce07e948f70964f8c` and 2,050 bytes. Its positional arguments are login, password, shared-secret, machine name, comma-separated app IDs, and minutes. It uses `steam-user` and `steam-totp`, logs on, requests free app licenses, waits 80 seconds, marks the persona online, and reports games played. A 15-second interval checks connectivity and elapsed duration. Exit meanings in the source are 0 for missing arguments, 1 for disconnected state, 2 for completed activity, and 4 for client error. “Free license” here is Steam app entitlement, not FSM Panel product licensing.

The trading script has SHA-256 `d010c676ce33adaee529a79fafc9501fd2ca39c3ca0794e90680ff7cb681302c` and 17,823 bytes. Arguments are login, password, shared-secret, identity-secret, trade-offer link, optional inventory selector, comment, proxy URL, and refresh token. Inventory defaults to `730/2`; `selected:<path>` reads an array of asset records from a local JSON file. It normalizes app/context/asset IDs, separates CS2 items from others, supports SOCKS/HTTP proxies, prefers a supplied refresh token, and falls back once to password/TOTP after token login failure. The connection path switches to WebSocket after 10 seconds without login/connect completion and has a further 20-second bound. It creates trade offers and requests mobile confirmation. Several successful completion paths use exit code 1; an adapter must not assume that conventional zero/nonzero shell success semantics apply. It emits refresh-token material to stdout in its original code, so any future adapter must capture and redact output intentionally. No embedded script was executed.

The Node contracts are much more directly recoverable than the obfuscated native orchestration. This does not establish that every script branch is reachable from every panel UI mode.

## Licensing boundary and remaining gaps

The reported product-license gate was not bypassed, disabled, or patched. `API_URL` consumers at `0x140be5190` and `0x140bec990` are Steam session restoration/HTTP helpers, as shown by `finalizelogin`, `steamLoginSecure`, `sessionid`, `COMMUNITY_URL`, and refresh-token references. They must not be presented as the FSM licensing service.

The signed-proof validator is recovered at `0x140706b70`, compiled name `tCzdOweDdAzH`, source line 4826. Its parameters are `self, body, endpoint, hwid, nonce`. Manual Ghidra pseudocode and helper-level instruction checks establish this contract:

1. Read proof text from `body['b']` and a hexadecimal signature from `body['s']`.
2. Verify RSA-PSS with SHA-256 over the UTF-8 proof, using a key-provider helper. Key values are not exported.
3. Split the proof using `split('#', 5)` into `where, who, granted, ts, echo, features`.
4. Require `where == endpoint`, `who == hwid`, `granted == '1'`, `echo == nonce`, and `abs(time.time() - int(ts)) < 200`.
5. Return `features` when all checks pass; return `None` for a failed predicate or an ordinary caught exception.

The equality, subtraction, absolute-value, and less-than helper meanings were checked against instructions and CPython 3.11 type/number-slot definitions. The proof validator is referenced by the cached-proof candidate `DGmQXUBZeTlz` at `0x140714980`, line 4860, and request candidates `xIVLTDZOwWWv` at `0x14081e0b0`, line 4903, and `YRmXLZMOkCcx` at `0x140836410`, line 4969. These request candidates also reference the encrypted-response helper `WvxVrYbCouBn` at `0x1408a9050`, line 5108. The separate response candidate `IoHigjaYXFID` at `0x14084e780`, line 5054, retains local names `panel_uuid` and `panel_secret`.

This recovers a cryptographic authorization boundary, but the complete startup denial path, service request encoding, endpoint selection, and feature interpretation remain unresolved. HWID display and signed collector-update metadata are separate observations. Embedded private-key material was discovered in raw constants and remains private; no secret value is included in this folder. No proof was forged, no remote license request was sent, and no original license check was changed.

Before claiming a compatible original-workflow implementation, future authorized stand work must verify the reconstructed contracts and resolve remaining lock semantics, process/window timing, batch cancellation/retry behavior, lobby state transitions, persistence formats, and side effects. Static evidence here does not demonstrate full source recovery or runtime parity.

## Subsequent bounded contracts — October 15

Later reviewed slices supersede some earlier open questions in this report.
Their precise evidence, new safety decisions, tests and remaining gaps are kept
with the implementation contracts:

- [Launch preparation and settings](../docs/launch-plan.md): argv/environment,
  profile/shader proposals, loginusers, resolution and flag derivation; no executor.
- [Video](../docs/video.md), [BES](../docs/bes.md), and
  [account configs/autoexec](../docs/account-config.md): recovered file and command
  semantics, with deliberate ownership/transaction boundaries in the reconstruction.
- [GSI configuration](../docs/gsi-config.md): indexed writer, sanitized template
  and probe-result port selection; real launch/listener integration remains open.
- [Input fragment](../docs/input-plan.md): additional anti-AFK timing, late power
  read and complete key-hold method; no scheduler or real input implementation.
- [Autoaccept traces](../docs/autoaccept-plan.md): immediate dispatcher, count and
  abandonment decisions, both click paths and title activation; no input executor.
- [Co-op accept caller](../docs/coop-accept-plan.md): ticket normalization,
  late gates, delay calculation and partial state effects; no co-op executor.
- [Co-op state helpers](../docs/coop-state-plan.md): global GSI menu predicate
  and cached-region refresh request; the actual refresh worker remains opaque.
- [Telegram](../docs/telegram.md): eight send/edit contracts, selected destination,
  proxy, receipt and fallback behavior; automatic event orchestration remains partial.

All of these observations are static. None establishes original runtime parity.
