# Parse a copied console log without game actions

`fsm_panel.adapters.console_parser.read_console_log` reads a caller-selected local
log copy and returns a typed observation. It does not discover files, write logs,
change accounts, update GSI, send messages, or authorize game input. A read-only
desktop tool is connected; the recurring game scanner remains unimplemented.

## Desktop

Open **Журнал → Копия игрового лога**, select a local copy, choose the intended
calendar year and press **Прочитать копию**. The displayed timestamps use the
host's local timezone. The year field initially shows the current year; archived
logs need their own year. The parser does not infer a year from file metadata.

An optional cache identifier is copied into both independent cache inputs for
this manual tool. Without it the fallback is the clearly labelled value 1, never
an authenticated account. The relay checkbox selects a fixed enabled snapshot,
with an initial lost time of zero. Clock readings use the host wall clock. The
Python API below supports separate cache observations and explicit time providers.

The tool shows the eight observed values, source byte range, diagnostic counts,
and the number of inert proposals. It never applies proposals. Displayed strings
are quoted and truncated to 256 characters; raw file contents are not printed in
the application journal. Failed, partial, and cancelled reads are distinguished.
New reads clear the previous result. Work runs outside the Qt thread; cancellation
and window close wait for the owned file worker, including when its public Future
has already been cancelled. A read already executing in the OS cannot be forcibly
interrupted. This tool is available without a lease and in simulation mode.

Choose the log's intended calendar year and local timezone explicitly. The native
log grammar contains neither a year nor a timezone. This example deliberately
uses a fixed UTC+05:00 conversion for a synthetic offline copy:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fsm_panel.adapters.console_parser import ConsoleParserContext, read_console_log

local_zone = timezone(timedelta(hours=5))
observation_time = datetime(2026, 10, 16, 12, tzinfo=local_zone).timestamp()
context = ConsoleParserContext(
    initial_account=None,       # First native cache observation: identity.
    tail_account=None,          # Independent second observation: read budget.
    fallback_account=1,         # Deterministic placeholder, never a validated account.
    clock=lambda: observation_time,
    year=lambda: 2026,
    local_timestamp=lambda value: value.replace(tzinfo=local_zone).timestamp(),
)
result = read_console_log(Path(r"C:\analysis-copy\synthetic.log"), context)
print(result.complete, result.issues, result.identity_origin)
# Inspect result.observation and result.proposals locally; they can contain
# private account strings, server tokens and timestamps. Do not log them publicly.
```

`complete` means the supplied input was processed without diagnostics. It does
not prove freshness, account ownership, a real game event, or permission to act.
`identity_verified` is always false. Even a parsed AuthStatus string is untrusted
log data. The fixed fallback identity is labelled `identity_origin="fallback"`
and adds `fallback_identity` to the diagnostics until replaced by an AuthStatus
extraction. No fallback identity is proposed for a cache write.

## Public API

`parse_console_log_tail(raw, context, *, linecount=1, cancelled=None,
max_lines=200_000)` parses supplied bytes without opening files. The caller must
supply an already selected tail within the context's byte limit. This function
cannot know whether omitted earlier bytes existed; its read offset is zero.

`read_console_log(path, context, *, linecount=1, cancelled=None,
max_lines=200_000)` selects and reads the tail from an explicit local regular file.
`linecount` is retained for characterization but has no effect, matching the
native argument. Both APIs accept an optional `threading.Event` for cooperative
cancellation between lines. A call already executing in the OS or an explicit
observation provider cannot be interrupted by that event.

The frozen `ConsoleParserContext` supplies:

| Field | Contract |
| --- | --- |
| `initial_account` | First cache result: exact string, integer, or None. A truthy value becomes the initial identity. |
| `tail_account` | Independent cache result whose truthiness selects the read budget. It is not inferred from the first result. |
| `fallback_account` | Exact integer from 1 through 99999, validated and used only if the first cache value is falsey. |
| `clock()` | Finite integer/float observation at entry, then at each successfully dated relay-loss comparison. |
| `year()` | Integer 1–9999, called afresh for each recognized timestamp branch with a date match. |
| `local_timestamp(naive_datetime)` | Explicit local-time conversion; returns a finite numeric timestamp. It must not silently assume that the source log was UTC. |
| `srt_enabled()` | Exact bool, observed on every line that reaches the relay stage, including trailing empty lines. Defaults to false. |
| `srt_lost_at` | Initial copied relay timestamp; later proposed writes advance only the parser's local copy. |
| `relay_fine` | Immutable tuple snapshot; defaults to `('ok', 'attempting', 'waiting', 'not attempted', 'retrying', 'unknown')`. |

Providers are explicit trusted dependencies. The parser invokes them at the
documented points; callers should supply observation-only functions. No machine
clock or timezone is consulted implicitly. `datetime.strptime` still uses
Python's date-validation behavior. The native reader instead consults live shared
state and host-local time conversion. Local copied relay state and a fixed
relay-status tuple are deliberate reconstruction choices. The same context can
be reused without being mutated; to model subsequent calls, a caller may create
a new context using reviewed proposals.

Initial cache inputs are restricted to strings of at most 4096 characters,
integers of at most 128 bits, and None. Arbitrary objects, subclasses, custom
truthiness, non-finite numbers and malformed provider results are unsupported.
The status tuple accepts at most 128 exact strings. Strict schemas and fixed
error codes are new boundaries, not native validation claims.

## Result and diagnostics

`ConsoleParseResult` contains `observation`, ordered `proposals`,
`identity_origin`, `read`, unique `issues`, `line_errors`, `missing_timestamps`,
and `legacy_fallback`. Sensitive fields are omitted from dataclass representations.
The typed observation provides `as_legacy_tuple()` in this exact order:

```text
acc, lastmatch, joined_party, pinging_server, last_match_time,
joined_menu_after_match, last_match_loading, last_main_menu_load
```

Normal empty input retains the supplied cached/fallback identity; its two flags
are Boolean False and the four remaining times are integer zeros. A file failure
returns the distinct compatibility tuple `('', '', 0, 0, 0, 0, 0, 0)`, with
integer zeros in every numeric slot and `legacy_fallback=True`. It also reports a
fixed failure diagnostic, so a failed read cannot look like a normal empty log.
Cancellation before opening a file also uses that compatibility shape, labelled
`cancelled`; application cancellation did not exist in the recovered reader.

`AccountCacheProposal(account, line_number)` and
`RelayLostProposal(timestamp, line_number)` are inert frozen records. Line numbers
start at one within the selected tail, including its possible partial first line.
Multiple proposals remain in order. A relay proposal precedes a same-line account
proposal. Nothing applies them to the caller's mappings or settings.

`ConsoleReadInfo` reports `byte_limit`, `bytes_read`, `source_size`, and `offset`.
Limits are exactly **2,000,000 bytes** for a truthy second cache observation and
**12,000,000 bytes** otherwise; these are decimal counts. Prefix omission,
invalid UTF-8, line errors, and identity uncertainty are separate diagnostics:

| Issue | Meaning |
| --- | --- |
| `tail_prefix_omitted` | The file is larger than the selected tail; earlier bytes were not parsed. |
| `invalid_utf8_ignored` | Invalid byte sequences were dropped, as in the recovered decoder. |
| `missing_timestamp` | A branch that originally printed a missing-date notice found no date. `missing_timestamps` counts those notices. |
| `line_error` | An ordinary error skipped the remaining branches on that line. Earlier mutations/proposals remain. |
| `line_limit` | The new work limit stopped parsing; the observation is partial. |
| `cancelled` | Cooperative cancellation stopped work; the observation/proposals may be partial. |
| `fallback_identity` | The returned identity remains the deterministic placeholder. |
| `file_unavailable_or_unsafe` | Opening/reading/closing or locality/type checks failed. |
| `source_changed` | File identity, size, modification time, parent identity, or expected read length changed. |

The default line budget is 200,000, configurable from 1 through 1,000,000. A
streamed split preserves exact `split('\n')` behavior without allocating a large
list for newline-heavy input. No line boundary, BOM, CR, or whitespace is trimmed.
The first partial line remains eligible for markers. A final empty line is
processed. Bytes larger than the selected limit are rejected by the bytes API.

Input/schema violations raise `ConsoleParserError` with a fixed code. An initial
clock failure raises `console_initial_observation_failed` before file work; it
does not return the file-failure tuple. Normal per-line provider/date failures
are diagnostic line errors. Exceptions outside `Exception` propagate, with file
context cleanup still attempted.

## Preserved grammar and quirks

All marker tests are case sensitive, in recovered stage order:

1. A received-ticket line uses the first ticket marker and the first `vport`
   anywhere in the line. It extracts from fixed offset 59, removes literal spaces,
   and appends the token before parsing its timestamp. Duplicate/empty tokens remain.
2. Only if step 1 did not match, a line containing both `vport 0` and
   `] Discarding inactive session` removes the first matching token extracted
   after the first `SDR server`, fixed offset 10. Removal does not reset time.
3. FriendJoin, exact MAINMENU-to-MAINMENU, MAINMENU-to-LOADINGSCREEN, generic
   arrow-to-MAINMENU, and refreshing-pings markers are independent branches.
   A menu-to-menu line therefore performs two timestamp conversions.
4. The relay stage observes enablement, tests `SDR RelayNetworkStatus:`, and uses
   `anyrelay=([A-Za-z]+(?: [A-Za-z]+)?)`. Captured words are lowercased and compared
   with the supplied allowed tuple. A missing match is not a loss.
5. AuthStatus uses the first `AuthStatus (steamid:` and first `):` anywhere, fixed
   offset 20, and removes only literal spaces. It returns an unchecked string and
   proposes a cache write, including when the extraction is empty.

Timestamp branches search for the first
`\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}` anywhere. Each branch obtains its
own supplied year and converts `%Y/%m/%d %H:%M:%S`. There is no freshness or
New Year correction. Later successful lines can move ordinary fields backward.
Relay proposals alone use `now + 60 >= candidate > previous`; the previous value
is consulted only after the future-bound comparison passes.

`lastmatch` is the last surviving ordered ticket, not the newest timestamp's
ticket. A discarded ticket can leave a nonzero `last_match_time` with an empty
or different surviving token. The entry clock minus 17 is observed but unused.
A missing ticket/relay date is silent; other timestamp branches add notice
diagnostics. A malformed date leaves earlier same-line changes intact and skips
the rest of that line.

## File and failure boundaries

The wrapper shares the discovery utility's lexical locality guard. It rejects
UNC/device/URL paths and Windows remote drives before any filesystem metadata
access, including reserved/alternate-stream syntax in the final filename.
Parent symlinks/reparse points and non-regular final files are rejected.

One bounded read uses the opened file's size and identity, followed by checks
for changes. This deliberately replaces the native path-based `getsize` race.
Short reads are diagnosed as source changes. Parsing occurs while the file is
open. If a later close or post-parse source check fails, the compatibility
observation becomes the failure tuple while already-created proposals remain.
Those proposals are incomplete and are never applied automatically.

The source printed tracebacks for per-line failures and returned the fallback
if traceback printing itself failed. This utility never prints or writes those
diagnostics; it reports fixed codes and counts instead. It does not emulate
printing failures or arbitrary shared-cache setter failures. There is no claim
of an atomic filesystem snapshot or protection against hostile cross-process
path replacement. Use copies under the operator's control.

## Evidence and checks

Native target: `Panel.exe` SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
Content reader: `oAjysWzxGvAh` at `0x140b2a590..0x140b3271d`, source line 33363;
relay predicate: `gkMLzjYOWcxT` at `0x140a19140`. The default `linecount=1`
is registered at `0x140c2141b..0x140c2143f`; the relay tuple is initialized at
`0x140c20123..0x140c20138`.

The sealed private sanitized checkpoint-06 contract has SHA-256
`254d1a5345327348ce176e752dca610cb3f4d2157b707c2c67f92ab93ee66b17`;
its manifest has SHA-256
`aee30501b2ed7456ed881d64565f13f917768afb12bd458cf949a373eb8e1b7f`.
Raw native material remains private. This contract is static reconstruction,
not recovered executable source or observed runtime parity.

```powershell
uv run --frozen pytest tests/adapters/test_console_parser.py tests/adapters/test_console_logs.py -q
uv run --frozen ruff check src/fsm_panel/adapters/console_parser.py tests/adapters/test_console_parser.py
uv run --frozen mypy src/fsm_panel/adapters/console_parser.py src/fsm_panel/adapters/console_logs.py
```

Tests use synthetic files and explicit timezone/year/clock providers. They check
ticket multiplicity and stale timestamps, overlapping markers, partial-line
errors, Unicode/CR retention, both tail limits, source changes, cleanup, locality,
diagnostic privacy and proposal ordering. No original application, real account,
Steam/Telegram service, GSI receiver or input executor is exercised.
