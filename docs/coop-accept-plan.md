# Recovered co-op accept caller

`fsm_panel.adapters.coop_accept_plan` describes the complete immediate co-op
accept caller as an inert, resumable trace. It performs no networking, logging,
state mutation, clock read, sleep, thread creation or input. A synthetic driver
supplies observations and acknowledges proposed effects in their recovered order.
The caller's scheduler and the co-op helpers remain dependencies.

The source evidence is static reconstruction of Panel.exe SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
The private sanitized checkpoint-03 SHA-256 is
`7a181252c803069b4a9ccfb56673cc3e8f5b2ad38476d07ae13a1c8fb7eabb0e`.
No original/extracted program, co-op service, Steam or game was executed.
Private decompilations and constants remain outside Git. The tests establish
this synthetic contract; they do not prove runtime parity or successful input.

## API and response protocol

`iter_legacy_coop_accept(coop_tickets)` yields frozen
`CoopAcceptRequest(operation, arguments, keywords)` records and returns None.
`keywords` preserves keyword argument order as a tuple of name/value pairs.
Operation names are descriptive strings; this module never dispatches them to
real functions. Send observations with `send(value)` and acknowledge effects
with None. `throw(exception)` propagates immediately, without later requests or
rollback. Closing the generator performs no cleanup or real effect.

```python
from fsm_panel.adapters.coop_accept_plan import CoopRef, iter_legacy_coop_accept

plan = iter_legacy_coop_accept((("synthetic-account", "server-token", 100),))
assert next(plan).operation == "read_coop"
assert plan.send(CoopRef("synthetic-coop")).operation == "read_launched_account"
request = plan.send({"steamid": "synthetic-id"})
assert request.operation == "write_coop_servers"
assert request.arguments == (frozenset({"server-token"}),)
plan.close()  # The set assignment was described, not performed.
```

An inert value means an exact builtin string, bool, integer, finite float, None,
or a recursively nested tuple of those values. Custom classes and subclasses are
rejected before their conversion, comparison or truthiness hooks can run.

| Request | Required response |
| --- | --- |
| `read_coop` | None, or exact `CoopRef` with a string token |
| `read_launched_account(login)` | Exact dict with string keys, or a supported false-valued builtin value |
| `observe_all_in_menu`, `observe_regions` | An inert value, passed through unchanged |
| `short_id(server)` | Exact string |
| `consume_accept(coop)` | Exact string or None |
| `read_setting(name)` | An inert value; normal builtin truthiness is used for gates |
| `accepted_games_contains(server)` | Exact bool representing the current membership result |
| `read_coop_servers` | Exact frozenset of exact strings |
| `read_time(purpose)`, `read_global("MIN_ACCEPT_DELAY")` | Exact integer or finite float, excluding bool |
| `construct_thread` | Exact `ThreadRef` with a string token |
| All other requests | None acknowledgement |

`CoopRef` represents identity, including a false-valued non-None legacy object.
An empty token is valid and does not mean absent. The captured reference is used
for report, consume and leave requests without rereading the global co-op object.
`ThreadRef` represents the constructed object so construction and start can fail
at distinct request boundaries; no actual thread handle exists.

## Ticket normalization and reporting

The first request reads co-op presence. None returns immediately without
inspecting ticket input. Otherwise tickets must be an exact tuple, with each row
an exact three-item tuple `(login, server, logged_at)`. Rows are inspected in
order. A false-valued server skips account lookup and the other field checks.
An included server and the account lookup key must be exact strings.

The account observation models `launched_accs.get(login)`. A false-valued result
acts like `{}`. A truthy result must be an exact dict with string keys. Only its
`steamid` value is interpreted; other values are opaque and never accessed.
A missing key defaults to `""`. An existing value must be a builtin scalar and
is converted with `str`, then the resulting string is truth-tested. Thus stored
None becomes `"None"`, zero becomes `"0"`, and neither is discarded. An empty
string is discarded. There is no numeric Steam ID validation.

For the outer account fallback, server skip and timestamp fallback, supported
false-valued inputs are None, False, numeric zero, `""`, and empty exact tuple,
list or dict. Nonempty non-dict account values are rejected. A false-valued
timestamp becomes integer zero. A truthy timestamp must be an exact integer or
finite float, excluding bool; timestamp strings are never parsed.

Normalized ticket pairs preserve input order and duplicates. The per-server
timestamp is the maximum of all included timestamps, starting from integer zero.
Negative timestamps therefore leave the initial zero unchanged. Timestamp
validation is skipped when the account produces no Steam ID.

After normalization, `write_coop_servers` describes assigning the set of included
servers. The all-in-menu and regions dependencies are observed even when no
tickets survive. A TICKETS log is described whenever the original tuple or the
normalized list is nonempty. Its found count uses the original tuple length; sent
uses the normalized length. Short IDs are requested for **every** normalized pair,
including duplicates, then deduplicated and sorted for that log.

Only the log replaces a false-valued regions result with `"any"`. `report_tickets`
receives `(captured_coop, normalized_pairs, in_menu, original_regions)` in that
order, where the first argument identifies the receiver. `consume_accept` follows
reporting, before any acceptance gate. There is no local all-in-menu acceptance
gate, and the dependency observation does not claim to implement that helper.

## Acceptance, refusal and timing

The gates execute in order: consumed server is None; current AUTOACCEPTGAME truth
value; current accepted-games membership. Each passing gate enables the next
observation. An empty consumed string is distinct from None and reaches the later
gates. There is no local catch, lock, wait or cancellation check.

The next observation reads the current server set. Equality must hold against
the entire singleton `{server}`. A set containing both the requested server and
another server refuses. On refusal, the trace requests the server's short ID,
rereads the current set, and requests each current short ID. Those results are
sorted **without deduplication** for ACCEPT_REFUSED. The log precedes
`leave_match(captured_coop, "match_cancelled")`; a log failure prevents leaving.
This order differs from the separately recovered dispatcher abandonment helper.

On the successful singleton path, a truthy local `seen_at[server]` causes a clock
request followed by subtraction of that local timestamp. Otherwise waited is
positive float `0.0`, with no age clock request. These are ordinary wall-clock
ages; a negative age is retained. The local dictionary cannot be externally
mutated through this API, so its two source-level gets require no extra request.

The delay expression is evaluated with two independent, ordered observations:

```python
outer_minimum = first_global_observation
inner_minimum = second_global_observation
delay = min(outer_minimum, max(0.0, inner_minimum - waited))
```

Checkpoint 03 establishes the initialized global value as **4.0**. The exposed
`LEGACY_INITIAL_MIN_ACCEPT_DELAY` constant records that evidence for test drivers;
the trace still requests both current global values. If both are 4.0, ages below
zero give delay 4, ages from zero to four give 4 minus age, and later ages give
zero. No age rounding occurs before this calculation. Changed finite global
values retain the expression, even if they produce a negative delay.

The dispatcher definition's default tuple captures `(4.0, None)` during function
construction. That captured default differs from this caller's late global reads.
This caller supplies both dispatcher arguments explicitly; it never relies on the
defaults and never reads ALTERNATIVE_AUTOACCEPTER itself.

## State changes, logging and the thread boundary

The trace describes appending the server to accepted games, reading time and
assigning LAST_AUTOACCEPTER_ACTIVATION, then reading time again and assigning
last_accepted_game. The two timestamp reads are independent.

Next it requests the server's short ID and ENABLE_AUTOACCEPT for the ACCEPTING
log. Only the displayed age and wait are rounded to one decimal. After the log,
ENABLE_AUTOACCEPT is requested again for the thread gate. A false second value
suppresses the application notice and thread, while all preceding effects remain
completed. A changed second value can enable the thread even if the log recorded
a false first value.

The thread construction request preserves exactly these keyword arguments:
symbolic target `self.VIXswEwxFlBY`, `args=(unrounded_delay, server)`, and
`daemon=True`. It follows the application notice and is followed immediately by
the separate `start_thread(ThreadRef)` request. The module does not resolve the
target, import threading, start workers, join, retry or retain a real handle.

Every effect acknowledgement represents a separate completion boundary. A report
failure prevents consume; an append followed by a clock failure leaves the
accepted marker; a later timestamp or log failure leaves earlier writes. Thread
construction/start failures likewise leave the marker and timestamps. The trace
does not undo these effects or swallow ordinary exceptions or BaseException.

## Evidence and reconstruction limits

The complete caller is `FxVStgbxQDJH.aGbDxKVrcUAw`, entry `0x140b43580`, source
line 33754. Useful sanitized anchors are:

| Behavior | Anchor |
| --- | --- |
| Initial co-op read | `0x140b43762` |
| Account lookup / ticket append / timestamp maximum | `0x140b43af5`, `0x140b43d77`, `0x140b43e0f` |
| Set assignment / all-in-menu / regions | `0x140b44e8e`, `0x140b44ef3`, `0x140b45066` |
| Report / consume | `0x140b45c32`, `0x140b45d1d` |
| Refusal log / leave | `0x140b463fc`, `0x140b464c2` |
| Age clock / two delay reads | `0x140b46677`, `0x140b4684d`, `0x140b468c6` |
| Timestamp writes | `0x140b46cb4`, `0x140b46e47` |
| Two ENABLE_AUTOACCEPT reads | `0x140b470d2`, `0x140b473ce` |
| Thread keywords / start | `0x140b476ce`, `0x140b47728` |
| Global 4.0 initialization / dispatcher defaults tuple | `0x140c212b5..0x140c212ca`, `0x140c214c8`, `0x140c214d6` |

Frozen records, strict schemas, explicit responses and validation failures are
new reconstruction boundaries. They exclude arbitrary Python objects, custom
mapping/get methods, iterator mutation, numeric coercion and non-finite inputs.
Derived arithmetic must also remain finite. Region/helper values use immutable
inert values without asserting their real runtime schema. A truthy bool timestamp
is rejected even though legacy Python numeric behavior could accept it.

Protocol failures raise `CoopAcceptPlanError` with the fixed message
`coop_accept_plan_response_invalid`. Validation occurs when a value is reached;
no rollback is added. Unexpected effect return values require conversion into a
None acknowledgement by a synthetic driver, since the original discards them.

Unresolved: the workflow that invokes this caller; other autoaccept callers and
event readers; helper internals, transport and policies; regions default max_age;
anti-AFK scheduling; account file queue completion; concurrency, runtime behavior
and window ownership. These traces do not authorize a network or input executor.

```powershell
uv run --frozen python -m pytest tests/adapters/test_coop_accept_plan.py
uv run --frozen ruff check src/fsm_panel/adapters/coop_accept_plan.py tests/adapters/test_coop_accept_plan.py
uv run --frozen mypy src/fsm_panel/adapters/coop_accept_plan.py tests/adapters/test_coop_accept_plan.py
```
