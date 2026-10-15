# Recovered co-op state helpers

`fsm_panel.adapters.coop_state_plan` describes two small helpers as inert,
resumable traces. It performs no global lookup, clock read, dictionary access,
state mutation, thread creation, networking or input. A synthetic driver supplies
observations and acknowledges effects. These helpers are not connected to a
scheduler or live co-op service.

The original Panel.exe SHA-256 is
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
Evidence is static native reconstruction. The private sanitized checkpoint-04 SHA-256 is
`c89217bd5f598e9d82e3f5ca4aaff27fd684face643ef162debec06468a0b671`;
its manifest SHA-256 is
`781670d254fb5fb456f1e06a85cbb302391dbb19f04583e58b95469d1d11c74b`.
No original or extracted program was executed. Private dumps remain outside Git.
Passing synthetic tests does not
establish runtime parity, helper network behavior or successful game input.

## Resumable request protocol

Both APIs yield frozen `CoopStateRequest(operation, arguments, keywords)` records.
The strings describe operations; this module never dispatches them to real
functions. Send a supplied observation with `send(value)`. Effects require None
acknowledgement. `throw(exception)` models a dependency failure at the suspended
request. The menu helper applies the recovered exception scopes. The regions
helper models builtin getattr defaults but has no source-level recovery handler.
Closing either generator performs no effect or cleanup.

`iter_legacy_all_in_menu()` returns bool through `StopIteration.value`.
`iter_legacy_regions(max_age=60.0)` returns the exact previously supplied
`RegionsRef` object through `StopIteration.value`.

```python
from fsm_panel.adapters.coop_state_plan import iter_legacy_all_in_menu

plan = iter_legacy_all_in_menu()
assert next(plan).operation == "game_info_length"
try:
    plan.send(0)
except StopIteration as completed:
    assert completed.value is False
```

| Request | Response |
| --- | --- |
| `game_info_length` | Exact nonnegative integer, excluding bool |
| `game_info_iterator` | Exact `IteratorRef` |
| `next_game_info_key(iterator)` | Exact `GameKey`, or `IterationEnd()` for normal exhaustion |
| `read_game_info_entry(key)` | Exact `GameEntry` for that individual lookup |
| `read_entry_path(entry, ("provider", "timestamp"))` | Exact integer or finite float, excluding bool |
| Other `read_entry_path` requests | Exact string |
| `read_time(purpose)` | Exact integer or finite float, excluding bool |
| `get_regions_cache(name, default_cache)` | Exact `RegionsCache`, or `CacheMissing()` |
| `get_regions_busy(name, False)` | Exact bool |
| `write_regions_busy(True)` | None acknowledgement |
| `construct_thread` | Exact `ThreadRef` |
| `start_thread(thread)` | None acknowledgement |

Every reference has an exact string token. Iterator, key and entry references
represent dependency identity without exposing executable objects. No dictionary
or window ownership is inferred from a token. Region references are opaque: their
contents and truthiness are never read. Repeated token labels do not replace the
identity of the supplied wrapper object, including fresh default wrappers.

## Global menu helper

The source first evaluates `len(GAME_INFO)` outside all handlers. Zero returns
False without obtaining an iterator or reading time. A length dependency failure
propagates. After a nonzero length, an outer handler covers iterator creation and
advancement; an ordinary exception there returns False.

For each key, a separate inner handler covers this ordered sequence:

1. Read wall-clock time, look up the current entry, and read its provider timestamp.
2. If age is strictly greater than 35 seconds, skip the rest of that entry.
3. Look up the current entry again and read its map name. Any underscore returns False.
4. Look up the current entry again and read player activity. Anything unequal to the exact string `"menu"` returns False.

The fresh clock occurs for each key. Map and activity do not reuse the entry from
the timestamp lookup. Path requests represent the nested lookups as a dependency
operation; a missing provider, map or player field can be modeled by throwing
KeyError at that request. There is no upfront snapshot of the mapping or entries.

Exactly 35 seconds is still checked. Negative ages are also checked because the
source uses wall-clock subtraction without a monotonic freshness policy. The map
condition is substring membership, not a map-name allowlist or case conversion.

An ordinary exception inside one entry is swallowed and iteration continues.
Normal exhaustion after a nonzero initial length returns True. Consequently,
nonempty data with all stale or malformed entries returns True; an iterator that
becomes empty after the initial length check also returns True. This result is a
recovered predicate, not a guarantee that any live client is currently in menu.

BaseException subclasses propagate through both handlers. `IterationEnd()` is the
protocol's explicit normal exhaustion signal; throwing StopIteration directly at
`next_game_info_key` also returns True, matching Python for-loop exhaustion.
StopIteration during iterator creation is an outer failure returning False, while
StopIteration inside an entry is swallowed with other ordinary entry errors.
Other ordinary exceptions at advancement return False. No actual iterator from a
caller is run by this module.

## Regions cache helper

The recovered definition default is `max_age=60.0`, exposed as
`LEGACY_REGIONS_MAX_AGE`. The first request describes
`getattr(self, "_srt_regions_cache", (0.0, []))`. Its default is represented as
`RegionsCache(0.0, RegionsRef("default-empty-regions"))`, with a fresh region
reference for each invocation. That reference describes the fresh empty list;
no mutable list or live cache is created. `CacheMissing()` selects this default.
A present but malformed cache value, including None, is not a missing attribute.
Throwing AttributeError at the cache observation also selects the default, exactly
as builtin getattr does. Other attribute-access errors propagate. The default is
not assigned to the cache attribute by this helper.

The supplied cache stamp and regions reference are captured before the clock
request. Only when `now - stamp > max_age` does the trace request
`getattr(self, "_srt_regions_busy", False)`. Throwing AttributeError at this read
selects False; other errors propagate. Exactly 60 seconds with the default
age performs no busy read or refresh. The parameter has no artificial nonnegative
bound: a negative explicit max_age retains the original comparison.

If expired and not busy, the trace describes assigning busy=True, constructing a
thread with exactly `target=self.bkcfWJfVrPJw` and `daemon=True`, then calling start.
No args keyword, join, retry, cache reread, local lock or busy reset is added. The
target is a symbolic string and is never resolved or invoked.
The construction dependency includes resolving the thread constructor and bound
worker after the busy write. Their failures can be injected at that request.

The function returns the earlier observed region object, even if the described
thread replaces the cache before start completes. The generator returns that
same `RegionsRef` instance, not a copy or a newly read reference. Region contents
may be mutable in the original; this API only preserves the observed identity.

There is no source-level catch or finally. A constructor or start failure after
the acknowledged busy=True assignment leaves that assignment completed. Later
failures do not clear busy, undo earlier effects or return the regions reference.
BaseException likewise propagates without cleanup.

## Static evidence anchors

The menu helper is the global `AgdcojomMaLk`, entry `0x140b70560`, complete range
`0x140b70560..0x140b7142f` (3,792 bytes). The similarly named class method at
`0x140a44770` is a different function and is not substituted here.

The regions helper is `App.SlCmLWXMDIIe`, entry `0x140834450`, with two chained
unwind ranges ending at `0x140834c7a` (2,091 bytes total).

| Behavior | Anchor |
| --- | --- |
| Length before outer handler | Entry through `0x140b70682` |
| Iterator acquisition / advancement | `0x140b706bf`, `0x140b70713` |
| Per-entry clock / timestamp path | `0x140b70763`, `0x140b70864`, `0x140b708de` |
| Strict age >35 | `0x140b709a2`, `0x140b709ac`, `0x140b709dc` |
| Underscore containment / activity inequality | `0x140b70a7e`, `0x140b70b30` |
| Inner handler / normal True / outer False | `0x140b70b58`, `0x140b7100d`, `0x140b71248` |
| Fresh cache fallback / getattr | `0x140834535`, `0x140834557` |
| Regions clock / subtraction / strict comparison | `0x140834749`, `0x140834801`, `0x140834830` |
| Busy read / truth test / write | `0x140834878`, `0x14083488c`, `0x1408348d7` |
| Worker lookup / thread keyword tuple | `0x14083497b`, `0x1408349db` |
| Thread start / old-value return | `0x140834a56`, `0x140834a92` |
| Proven 60.0 definition default | Registration slice `0x140c1b653..0x140c1b67c`, constructor `0x1405e2e00` |

The default-registration slice SHA-256 is
`8b26057a3ce957d3df903c91342b791272d7dbcede10b8da9c952b5990ce75e7`.
Fresh-list construction and AttributeError-only getattr fallback are independently
corroborated by the checkpoint's bounded copy/getattr helper traces.

## New API boundaries and unresolved behavior

Exact builtin types, frozen request/reference records and explicit dependency
responses are reconstruction choices. Custom objects, subclasses, arbitrary
mapping methods, numeric coercion and truthiness hooks are rejected. Busy accepts
only bool, although the original uses ordinary Python truth testing. Numeric
observations and derived age arithmetic must remain finite. Map and activity
values must be strings; malformed legacy operations should be modeled with
dependency exceptions rather than invalid protocol values.

Invalid responses raise `CoopStatePlanError` with fixed message
`coop_state_plan_response_invalid`. This exception always propagates, including
inside both original menu catch regions. Thus protocol failures are deliberately
distinct from the original's swallowed ordinary dependency failures. Cache scalar
validation and max_age validation can reject malformed values earlier than the
legacy's arithmetic; they are not claimed as original validation.
An unhandled StopIteration injected at another generator boundary is subject to
Python's RuntimeError conversion for generators. Normal menu-loop exhaustion is
handled explicitly as described above.

The region cache record represents the source two-value unpacking. Arbitrary
cache iterables and their side effects are excluded. Identity tokens represent
objects only; these traces neither inspect external mutable state nor establish
thread safety. The refresh target's body, transport, cache population, scheduler,
account-file queue and other co-op policy remain unresolved.

```powershell
uv run --frozen python -m pytest tests/adapters/test_coop_state_plan.py
uv run --frozen ruff check src/fsm_panel/adapters/coop_state_plan.py tests/adapters/test_coop_state_plan.py
uv run --frozen mypy src/fsm_panel/adapters/coop_state_plan.py tests/adapters/test_coop_state_plan.py
```
