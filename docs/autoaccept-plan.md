# Recovered autoaccept traces

`fsm_panel.adapters.autoaccept_plan` lets tests inspect recovered autoaccept
decisions and effect ordering. It never executes input, sleeps, reads a clock,
acquires locks, opens account files, logs, or contacts a co-op client. No UI,
workflow, scheduler, or real input adapter is connected to these generators.

The original is Panel.exe SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
The contract comes from bounded, read-only native analysis. The private sanitized
checkpoint-02 SHA-256 is
`f982fd546442e3d61972fa4d75cb50ca2d995793105ed020354caf7f8c7bfbf5`.
Registration/default observations additionally use checkpoint-03 SHA-256
`7a181252c803069b4a9ccfb56673cc3e8f5b2ad38476d07ae13a1c8fb7eabb0e`.
Only behavioral anchors are included here; private dumps remain outside Git.
Synthetic tests establish the reconstruction contract, not successful CS2 input
or runtime parity with the original executable.

## Consume an inert trace

Each generator yields a frozen `TraceRequest(operation, arguments, lock_depth)`.
Supply the requested observation with `send(value)`. Send `None` to acknowledge
an effect; acknowledgement does not establish input, focus, or message success.
No operation-name string is dispatched to a real function by this module.

```python
from fsm_panel.adapters.autoaccept_plan import iter_legacy_dispatch

trace = iter_legacy_dispatch(first_delay=1.5, server=None)
assert next(trace).operation == "print_accept"
assert trace.send(None).arguments == ("dispatch_start",)
assert trace.send(100).arguments == ("ALTERNATIVE_AUTOACCEPTER",)
assert trace.send(False).arguments == (1.5,)
assert trace.send(None).operation == "check_abandonment"
trace.close()  # No lock, timer, input, logging, or cleanup effect is executed.
```

The dispatcher leaves child operations as requests. A trace-comparison driver can
expand them using the corresponding generator, then return its decision or
acknowledgement. There is deliberately no executor in this module.

| API | Result | Deferred observations |
| --- | --- | --- |
| `iter_legacy_dispatch(first_delay, server)` | `None` | Start time, alternative setting, abandonment and row-count decisions |
| `iter_legacy_row_count(started)` | Boolean | Account rows, then 2v2 setting when count is below six |
| `iter_legacy_abandonment(server)` | Boolean | Current server set, co-op presence, short IDs, refreshed set for logging |
| `iter_legacy_background_batch()` | `None` | Screen, cursor, enumerated windows, supplied shuffle, mode per window, MAKELONG results |
| `iter_legacy_foreground_pixels()` | `None` | Account/window identities, late properties, time, pixels, mode and CS_HEIGHT |
| `iter_legacy_title_activation(title)` | `None` | Window handle on each attempt |

The Boolean helper results are available as `StopIteration.value`. A driver can
use `yield from` to obtain them without exhausting the generator into a list.
`lock_depth` describes the operation's own original nesting of `global_lock`,
which was an RLock. A standalone child trace excludes locks held by its caller.
The trace does not emit lock-enter/exit calls or emulate lock implementation
errors. The original context managers exit acquired locks on exceptional paths.

## Immediate dispatcher

The original function is registered with defaults `(4.0, None)`. The delay
comes from `MIN_ACCEPT_DELAY`, captured when the function is constructed;
subsequent global changes do not change that stored default. Initialization is
at `0x140c212b5..0x140c212ca`, registration at `0x140c2148e..0x140c214dd`.
The trace API deliberately requires both parameters explicitly. The recovered
co-op caller supplies an explicitly computed delay and server instead of relying
on these defaults.

The trace describes printing the accept notice and capturing wall-clock start
time before the outer lock. It reads `ALTERNATIVE_AUTOACCEPTER` under that lock.
The normal path applies `first_delay` only when strictly greater than zero.
It checks abandonment before batch one, then repeats a four-second delay and
abandonment check before batch two.

Before batches three and four, the order is four-second delay, row-count check,
abandonment check, batch. A true count result stops immediately and skips the
abandonment helper's side effects. The count helper receives the original start
time, including time spent waiting for the lock and initial delay. Each batch
reacquires the same RLock; the outer lock remains held across every delay.

The alternative path ignores `first_delay`, both decision helpers and the batch
schedule. It reacquires the lock, describes BlockInput(True), one foreground pixel
operation, and BlockInput(False). Only an ordinary exception from the pixel call
is caught; it describes traceback printing before unblocking. A block-call error,
traceback-print error, or BaseException propagates. Unblocking is not a finally
action. Normal-path errors propagate without dispatcher recovery.

Anchor: `VIXswEwxFlBY`, entry `0x140b39620`, source line 33625. Count-call anchor
`0x140b3a8a4`; loop abandonment `0x140b3ab93`; alternative block/pixel/unblock
`0x140b3b7f4`, `0x140b3b84a`, `0x140b3bb8b`.

## Decision helpers

The row helper visits **all** rows and counts `row[6] > started`. Equality does
not count. It returns true at six rows, without reading `RUNNING2VS2`. Otherwise
it reads that setting and requires both a truthy setting and at least two rows.
Malformed later rows still fail after six earlier qualifying rows. There is no
clock read, sleep, account-set filter, early count exit, or exception handler.
Anchor: `joSpnVFMKedd`, `0x140b72b10`, source line 34625.

Abandonment returns false for `server is None` without reading current servers.
It also returns false when the entire current set equals `{server}`. Membership
alone is insufficient: `{server, another}` abandons. Otherwise it reads whether
the co-op object is present and conditionally describes
`leave_match("match_cancelled")`. It then requests the supplied server's short ID,
reads the current server set again, obtains each short ID, sorts those results,
and describes an `ACCEPT_ABANDONED` log before returning true.

`read_coop_present` represents `COOP is not None`, independent of object
truthiness. `log_accept_abandoned` arguments represent the `server` and `current`
keyword values respectively. Errors in leave or logging prevent a true return.
There is no retry, local lock or cancellation check. Anchor: `XsAbtkjurpnI`,
`0x140b47b70`, source line 33822; short-ID generator `0x140602cc6`, `0x140602cd2`.

## Background click batch

The batch describes BlockInput(True), screen-size and cursor-position reads,
then cursor movement to `(screen_width - 1, current_y)`. It requests windows whose
titles contain the case-sensitive substring `Counter-Strike 2`, a single shuffle,
and a 0.15-second delay. Title enumeration remains a dependency request; no local
OS enumeration or process filter is provided.

For each shuffled `(handle, title)` pair, it describes printing that pair, then
reads `RUNNING2VS2`. The following calls retain the recovered order:

| Message | wParam | MAKELONG arguments |
| --- | --- | --- |
| `WM_MOUSEMOVE` | `0` | `(201, 113)` for 2v2; otherwise `(175, 115)` |
| `WM_LBUTTONDOWN` | symbolic `VK_LBUTTON` | `(0, 0)` |
| `WM_LBUTTONUP` | Python **`None`** | `(0, 0)` |

Every MAKELONG call is a separate deferred request; its supplied integer becomes
the next message's lParam. The symbolic constants are not interpreted by this
module. Whether pywin32 accepts/converts the None wParam is unresolved dependency
behavior. Each window ends with a 0.1-second delay.

Any ordinary effect exception inside the batch skips the entire remaining batch,
then describes BlockInput(False). BlockInput calls sit outside that handler.
BaseException bypasses the explicit unblock, and there is no guaranteed button
release. The original has no local lock, handle validation, visibility or owner
check, foreground check, activation, cursor restoration, or retry.

Anchor: `WrapBKBKoCkg`, `0x140b25120`, source line 33302. None wParam is anchored
at `0x140b2650c`, `0x140b265cd`, `0x140b265db`. The title-enumeration dependency
is `niOzZEGbTaRc` at `0x140a1f0c0`, callback `0x140a1f460`.

## Foreground pixel operation

The first request supplies launched-account identities in iteration order. Each
account's configured title is requested when that account is reached, followed
by matching window objects represented as `PixelWindow` tokens. Tokens preserve
duplicates and identify observations; they are neither HWNDs nor ownership proof.

For each window, the operation reads its title, describes activation, delays 0.15
seconds, then reads left, top, width and height in that order. These geometry
values remain local for that window; width is read but unused. The activation
result is discarded, so subsequent requests do not establish successful focus.

Only the first available window is polled. Empty earlier matches do not consume
the poll. The local flag becomes true before entering the polling loop, including
when the first elapsed-time observation immediately times out.

The loop tests supplied `time.time() - start < 7`, then describes a 0.1-second
delay, reads `CS_HEIGHT`, and samples `(x + 158, y + 113 + height - CS_HEIGHT)`.
Only green component index 1 is compared, strictly greater than 88. Success reads
time again for the notice and adds a two-second delay. The condition runs before
the delay and pixel read, so a final sample can occur after seven seconds. Clock
jumps retain wall-clock behavior; no monotonic deadline is substituted.

Timeout still proceeds to a click. Each window reads `RUNNING2VS2`, then reads
`CS_HEIGHT` again for cursor movement. Click x is `x + 201` in 2v2 and `x + 158`
otherwise; sample x remains `x + 158` in either mode. Movement is followed by
0.05 seconds, argument-free mouseDown, 0.14 seconds, and argument-free mouseUp.
Subsequent windows follow this click sequence without polling.

Any effect exception aborts the operation. There is no local unblock, mouse-up
finally, cancellation, owner verification, foreground restoration or deduplication.
The dispatcher's ordinary-exception catch can describe unblocking, but that does
not release a held mouse button. Anchor: `IgaMWMwCuHmI`, `0x140b360d0`, line 33596;
mode read `0x140b373f4`; mouseDown/delay/mouseUp `0x140b379ec`, `0x140b37a90`,
`0x140b37b3f`.

The separate activation trace describes three attempts under its own lock:
FindWindow(None, title), SetForegroundWindow(handle), then ShowWindow(SW_RESTORE).
An ordinary failure delays 0.2 seconds, including after attempt three. It never
checks nonzero handles or success values. Success and exhaustion both return
None. Delay errors and BaseException propagate. Anchor: `waNOjiACHTHT`,
`0x140a1c4e0`, line 27670.

## Reconstruction boundaries and unresolved integration

Frozen request records, explicit observation responses and validation are new
API design choices. The original accepts arbitrary Python values and uses normal
truth testing; this API accepts exact bools for flags and decisions. Numbers are
exact builtin integers or finite floats; geometry and packed messages require
integers. Strings are exact builtin strings. No custom coercion, formatting,
comparison or truthiness hooks are invoked.

Collections must be exact tuples, or frozensets of strings for server sets.
WindowRef/PixelWindow responses must be those exact record types with valid scalar
fields. A shuffle must preserve every original window and duplicate. Pixel
responses require exactly three integers; their range is not constrained. Row
responses require tuples with a numeric seventh column; other columns are opaque
and never interpreted. Account identities are strings, not the original mapping
objects. These supplied snapshots deliberately exclude custom containers,
concurrent iterator mutation and library-specific window-object behavior.

Effect results require a `None` acknowledgement even when the original discarded
arbitrary return values. Invalid protocol values raise `AutoacceptPlanError` with
the fixed message `autoaccept_plan_response_invalid`. They always terminate the
trace, including inside legacy ordinary-exception regions. Use `throw(Exception)`
to model a dependency failure and its recovered handler. `close()` causes no
cleanup request or real effect. Numeric `first_delay` validation happens before
any request, including the alternative branch; that validation is not legacy
behavior. Finite delays have no artificial upper bound.

The exact contracts above cover immediate operations. Their descriptive names do
not establish scheduler cadence, account file queue completion, or real library
behavior. Checkpoint 03 also recovers one co-op caller (`0x140b43580`), including
ticket normalization, state updates and explicit delay/server arguments. That
caller is implemented separately in [the co-op trace](coop-accept-plan.md);
the larger event/log reader, another
caller and helper internals remain unresolved. No inferred scheduler or ownership
policy is implemented here.

A future executor needs a separately specified window ownership and focus
contract, cancellable waits, and deliberate release/unblock cleanup. These would
be new behavior. Synthetic traces cannot establish ownership or actual game input.

Run the focused checks from the repository root:

```powershell
uv run --frozen python -m pytest tests/adapters/test_autoaccept_plan.py tests/qa/test_autoaccept_boundaries.py
uv run --frozen ruff check src/fsm_panel/adapters/autoaccept_plan.py tests/adapters/test_autoaccept_plan.py
uv run --frozen mypy src/fsm_panel/adapters/autoaccept_plan.py tests/adapters/test_autoaccept_plan.py
```
