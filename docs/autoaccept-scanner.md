# Recurring autoaccept scanner: recovered contract

This is static documentation, not an implemented scheduler or proof of game
parity. The original was not run. `YxuvSgOKpisR(self)` at `0x140b48440` is the
25,561-byte caller that links console logs to the already recovered dispatcher.
The target is the same Panel.exe SHA-256 recorded in `research/STATIC_FINDINGS.md`.

## Iteration and data flow

1. At startup, load `launched_accounts.json`, discover logs under
   `CSGO_PATH + '\\game\\csgo'`, and find sandbox `localconfig.vdf` candidates.
   Initialization errors propagate.
2. Each loop reads `COOP_MODE`. Co-op waits on `ACCEPT_WAKE` for up to 2.5 seconds
   and clears it; ordinary mode sleeps 2.5 seconds. These operations are outside
   the main catch. The mode is read again later before dispatch.
3. Increment the iteration counter, then check a stop predicate. Its recovered
   expression is `(_vk_buf[0] ^ _vk_buf[1]) != 0`; the buffer's writers/lifecycle
   are unresolved. It must not be assumed equivalent to a user stop command.
4. At counter `>= 5`, reset to zero and reload accounts/log paths. Refresh is per
   iteration count, not a guaranteed 12.5-second interval. When sandbox-log count
   is at least flat-log count, rescan localconfig paths and retain full UTF-8 file
   text keyed by the numeric `userdata` path ID. A nonempty scan does not clear
   old keys first. Empty results, or fewer sandbox logs, replace the cache with
   `{}`. Earlier writes survive partial failure.
5. Clear `ACCOUNTS_DATA_FROM_FILES` and local lists; shallow-copy `consolefiles`.
   Parse each path with `oAjysWzxGvAh(path, linecount=1)`; only `PermissionError`
   from that call skips a file. Append the row in the order
   `(tmp[0], tmp[2], tmp[3], tmp[1], tmp[4], tmp[5], tmp[6], tmp[7])`.
6. For each result, `tmp[1]` supplies a server token. Update
   `LAST_SERVER_SEEN_AT` only when `tmp[4] <= now + 60` and it exceeds the previous
   timestamp. The bound is **plus** 60 seconds, not a past-minute freshness filter.
   Co-op tickets use `(splitext(basename(path))[0], tmp[1], tmp[4])`; this differs
   from discovery's removal of all `.log` occurrences.
7. If there are parsed pairs, select `Counter(tokens).most_common(1)[0]`. Counts
   are occurrences, not verified unique accounts. There is no extra deduplication
   or tie-break rule. Late co-op mode forwards all tickets to its recovered caller
   and skips the ordinary gates below.

## Ordinary acceptance gates

The caller requires `AUTOACCEPTGAME`, a selected token unequal to `''`, and
either at least ten occurrences or at least four with the current launched-account
count between four and six inclusive. The latter reads length separately for each
bound. The ten-occurrence route does not inspect launched-account length.

An already accepted token skips the iteration. An ordinary error in that membership
check is printed and **continues toward acceptance**. Then the caller checks the
global `AgdcojomMaLk(GAME_INFO)` and `len(GAME_INFO) > 3`. This is the already
recovered global menu predicate at `0x140b70560`, not the similarly named class
method at `0x140a44770`; see [the helper contract](coop-state-plan.md). The old
predicate can accept nonempty telemetry containing only stale/malformed entries.
That legacy quirk is not a sufficient new safety rule for real input.

The caller writes `LAST_AUTOACCEPTER_ACTIVATION`. If `ENABLE_AUTOACCEPT` is truthy,
it logs and starts a daemon thread targeting `VIXswEwxFlBY` without arguments,
therefore using its established `(4.0, None)` defaults. It then appends the token
to `accepted_games` and sets `last_accepted_game`. **When input is disabled, those
bookkeeping writes still happen.** Starting a thread is not evidence of a click
or successful acceptance. No thread handle, join or rollback is retained.

The broad loop-body catch writes a traceback to `!aacepter.txt` in overwrite mode,
then sets the counter to eight, forcing refresh on the next normal iteration.
Errors writing that diagnostic propagate. Earlier mutations remain. There are
additional narrower catches around qmm reading, Counter-result extraction and
accepted-token membership. No scanner-level lock is acquired.

## Evidence and next dependencies

Private checkpoint 05 SHA-256:
`b3b163d97dc89fe0b4537d61fb7b1e48dbb229377c6e90918b1e5ecf16a3ed51`.
Manifest SHA-256:
`2dfed847be5bcf763554e53f56592933e23f9221e4b76b21415e8e2bb28eb769`.
It includes native anchors, constructor defaults, exact catch scopes and hash
checks of 183 prior artifacts. Its initially conservative description of the
menu dependency is superseded by the resolved global identity above: module
lookup `0x140b4cd6e`, one-argument call `0x140b4ce43`, checkpoint 04 body.

[Read-only discovery](console-logs.md) is implemented independently with explicit
local roots and bounded scanning. The content parser at `0x140b2a590`, launched
account loader, `find_all`, stop-buffer lifecycle and real window/process ownership
still require their own contracts before the complete workflow can be enabled.
