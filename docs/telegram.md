# Telegram contracts and owned async transport

`fsm_panel.notifications` provides a single-attempt Telegram client and separate
pure characterizations of the selected FSM_PANEL 3.1.2 bodies. Nothing in this
slice sends notifications automatically, changes the GUI, contacts Steam, or
downloads item icons. Tests use synthetic `httpx.MockTransport` responses only.

## Try the API offline

This complete example never opens a network connection:

```python
import asyncio
import httpx

from fsm_panel.notifications import TelegramClient


async def main():
    def reply(request):
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 71}}
        )

    transport = httpx.MockTransport(reply)
    async with TelegramClient(
        "12345:SYNTHETIC_NOT_A_REAL_TOKEN", transport=transport
    ) as client:
        result = await client.send_text(-101, "<b>Example</b>", thread_id="7")
        print(result.state, result.message_id)


asyncio.run(main())
# sent 71
```

The client owns its HTTPX client and transport, including an injected mock. Use
one event loop and `async with`, or explicitly await `aclose()`. A borrowed client
or transport is not supported. Each operation is a separate owned task;
`pending_count` exposes only the count. `aclose()` rejects new submissions,
cancels all pending operations, joins them, then closes HTTPX. Repeated close
calls are safe. `is_closed` is set after HTTPX has closed.

## Public methods and results

| Method | Proven request shape used |
| --- | --- |
| `send_text(chat_id, text, *, thread_id=None, html=True)` | `sendMessage`, HTML and optional topic. `html=False` selects the plain bot wrapper; a topic is then rejected. |
| `send_photo_paths(chat_id, caption, images, *, thread_id=None)` | One `sendPhoto` request with repeated `photo` parts, each filename `photo` and MIME `image/png`, regardless of suffix. HTML and optional topic. |
| `send_photo_bytes(chat_id, photo_bytes, caption, *, reply_to_message_id=None)` | One `sendPhoto` request with direct bytes as the multipart value, optional reply, no HTML or topic. |
| `edit_text(chat_id, message_id, text)` | `editMessageText`, HTML, no topic or reply. |
| `edit_caption(chat_id, message_id, caption)` | `editMessageCaption`, HTML, no topic or reply. |

All methods return an immutable `DeliveryResult`:

| `state` | Meaning | Caller action |
| --- | --- | --- |
| `not_sent` | Local validation/file failure, closed client, or a connection/pool failure before application delivery. | Resolve the cause. A new attempt remains a caller decision. |
| `sent` | A successful JSON response contains Boolean `ok=true` and a positive integer `message_id`. | Retain the message ID. |
| `rejected` | A decoded non-redirect response explicitly contains Boolean `ok=false`. | Surface the rejection; no built-in retry follows. |
| `unknown_outcome` | Dispatch began, but no reliable success/rejection was obtained. | Retain the uncertainty. Do not infer permission to resend or send a text fallback. |

`code` is a `DeliveryCode` enum. Optional `http_status` and `error_code` contain
bounded numeric metadata; remote descriptions, bodies and exception text are
never returned. An edit succeeds only if its returned ID matches the requested
ID. A truthy non-Boolean `ok`, missing/malformed ID, mismatched edit ID, malformed
JSON, non-success HTTP status carrying `ok=true`, or a redirect is uncertain.
Redirects are never followed. A valid `ok=false` response can reject at HTTP 200
or at an error status, including 429; `retry_after` is not acted upon.

There is no retry, photo-to-text fallback, important-bot forwarding, daemon
fanout, or automatic item-producer repeat in this client. Repeated method calls
are separate attempts, even with identical content. Telegram offers no client
idempotency key in these recovered calls. Durable operation storage,
deduplication, reconciliation and user-approved retry decisions belong to a
future caller; this slice provides no exactly-once guarantee.

### Cancellation and cleanup

`TelegramCancelled` subclasses `asyncio.CancelledError` and carries `.result`.
Cancellation continues to propagate; it is never converted into an ordinary
successful return. Cancellation before dispatch carries `not_sent`. Cancellation
after dispatch carries `unknown_outcome`, unless a validated result was already
received; that known result is retained during cleanup cancellation. A coroutine
cancelled before it ever starts cannot attach a result and has normal asyncio
semantics.

Files opened by path-photo operations close on success, failed opening of a later
file, timeout and cancellation. Response close runs in an owned shielded task.
Repeated cancellation waits for that task instead of abandoning it. Client close
waits for request cleanup before transport close. Cancelling the `aclose()` waiter
also waits for cleanup, then propagates cancellation.

Response streaming bypasses HTTPX's auto-close iterator. In pinned HTTPX 0.28.1,
`Response.aclose()` sets `is_closed` before awaiting stream close, so cancellation
during auto-close could otherwise leave cleanup unfinished. The client requests
`Accept-Encoding: identity`, accepts only identity response encoding, and reads
at most 64 KiB before returning an uncertain result. Unexpected compression is
uncertain. Response and transport cleanup are awaited even when this extends
beyond the request deadline.

## New validation and security decisions

These are reconstruction decisions, not claims about the old application:

- The origin is fixed to `https://api.telegram.org`. The token must match an
  ASCII decimal bot ID, a colon, and ASCII letters/digits/underscore/hyphen.
  Tokens cannot inject URL components. There is no base-URL override.
- TLS certificate verification is enabled. Environment proxies and environment
  trust settings are disabled with `trust_env=False`. Connection retries are
  explicitly zero. Production transport cannot be replaced by an arbitrary
  transport or supplied HTTPX client; the injection seam accepts MockTransport
  only for offline tests.
- `RequestTimeouts` sets connect/read/write/pool timeouts to 5/40/40/5 seconds
  and an independent request/body deadline of 60 seconds. Each configurable
  value must be finite, positive and at most 120 seconds. Synchronous local file
  preparation and mandatory close/join cleanup are outside that deadline.
- Chat IDs must be nonzero signed integers within the open interval
  `(-2**63, 2**63)`. Booleans and username destinations are rejected. Message and
  reply IDs must be positive integers below `2**63`. Topics may also be ASCII
  decimal strings of at most 20 characters, with the same positive bound;
  accepted topic strings pass through unchanged. Colons and whitespace in topics
  are rejected by the client even though legacy planners retain them.
- Text/caption must be UTF-8-encodable strings without NUL, at most 65,536 code
  points. Text cannot be empty; captions can. This is a local allocation bound,
  not a claim about Telegram's service limits. There is no escaping, splitting
  or truncation: callers deliberately supplying HTML retain responsibility for
  that markup.
- Path uploads accept a list or tuple containing 1–10 local paths. Each opened
  file must be regular and have a nonzero size no greater than 10 MiB at preflight.
  Byte uploads have the same size bound. The bytes are not decoded as images.
  Empty lists are rejected locally, without an implicit text send.

`TelegramConfigurationError` contains only a `ConfigurationCode`; it never wraps
raw exception text for display. Public request/configuration objects exclude
token, proxy URL, content, photo bytes, local paths, and legacy destinations from
their representations. Explicit request-data attributes remain sensitive: callers
must not log them or serialize them into diagnostics.

HTTPX normally logs full token-bearing URLs. A task-local `ContextVar` filter
suppresses this client's HTTPX and httpcore log records during request, setup and
cleanup. Unrelated requests, tasks and application logging remain enabled. The
filter covers the logger names in pinned HTTPX 0.28.1 / httpcore 1.0.9; recheck this
boundary on dependency upgrades. The client has no diagnostic event hooks.

### Proxy support

Pass `ProxySettings(url)` explicitly; only `None` disables the proxy. Empty or
malformed configured values raise a content-free configuration error. No invalid
proxy silently becomes a direct connection. Conventional `http://` and
`https://` proxy URLs require an explicit port in `1..65535`, a valid ASCII DNS
name/IP address, and no path other than `/`, query or fragment. Bracketed IPv6 and
percent-encoded credentials are supported; controls, malformed percent escapes,
multiple unescaped `@` characters and an empty username are rejected.

SOCKS and legacy `host:port:user:password`/reversed-`@` syntax are unsupported by
the new transport. They remain characterized by the separate legacy parser.
No optional SOCKS dependency was added. A configured proxy and an injected mock
cannot be combined, preventing HTTPX proxy routing from escaping the offline
transport. Tests inspect the proxy construction through a patched transport
factory that returns MockTransport; they never contact a proxy.

## Pure legacy characterization

`fsm_panel.notifications.legacy` contains no HTTP, files, threads, logging,
clock reads, credential lookup or execution. Request plans never contain a bot
token or request URL. Their `verify_tls=False` and scalar timeouts describe the
old request call; the new client does not execute these plans.

| Entry point | Characterized behavior |
| --- | --- |
| `threaded_destination_components` / `parse_threaded_destination` | Split every comma without filtering/stripping; colon unpacking requires exactly two parts; only chat ID uses Python `int`. |
| `receipt_destination_components` / `parse_receipt_destination` | Strip and drop empty comma components; split only the first colon; additional colons remain in the topic. |
| `parse_legacy_proxy` | Default HTTP scheme, whole-string strip only, exact `@` numeric-port heuristics, ignored third unadorned component, unencoded credentials, no general port/scheme validation. Returns `LegacyProxy.proxies` or `None`. |
| `build_legacy_request` | Eight sender/edit shapes, form fields, repeated path-photo descriptors or direct-byte descriptor, scalar timeout and disabled TLS flag. |
| `evaluate_legacy_response` | Receipt truthiness/indexed ID, edit Boolean policy, legacy photo accepts any decoded JSON, legacy text discards JSON, bot wrappers retain decoded JSON. Ordinary failures are characterized as caught or propagating, with legacy logging targets as data. |
| `selector_first_kind` / `selector_text_fallback` | `images is None` selects text; even `[]` selects photo. Falsey photo result selects one text attempt. |
| `receipt_first_kind` / `receipt_text_fallback` / `accepted_legacy_receipt` | Falsey images select text; only ID `is None` selects fallback or excludes a receipt. `0`, `False` and empty-string IDs are retained. |
| `inactivity_decision` | Unless repeating is truthy, suppress when both old and new messages contain case-sensitive `Panel has not sent any messages`. Otherwise return the new state for assignment before delivery. The supplied timestamp is not a timeout test. |
| `important_decision` | Return the filter with the shorter inactivity marker appended when needed, then case-sensitive substring matching. An empty filter element matches every string. The caller applies the returned expansion; the helper does not mutate input. |
| `format_item_message` | Add two newlines before the first item; append `price + " | " + name + "\n"` per item. Raw HTML is unchanged. A failed upstream price lookup can supply the proven literal `unknown`. |
| `producer_repeats_fanout` | Characterize the item producer's falsey-return check, including the possible duplicate after a normal `None` daemon-fanout return. This is not proof of delivery failure. |

New helper boundaries use typed plain-string/int/bytes inputs; arbitrary legacy
objects are not supported. Destination/proxy parsing errors become the sanitized
`LegacyInputError("invalid_legacy_input")`. The legacy parser accepts only string
or `None`; other falsey object types are outside this public boundary. Callable
defaults in this new API are conveniences, not recovered native defaults.

| Operation | Method | Timeout passed to old Requests call | JSON success rule |
| --- | --- | ---: | --- |
| `RECEIPT_PHOTO` | `sendPhoto` | 40 | Truthy `ok`, then indexed `result.message_id` |
| `RECEIPT_TEXT` | `sendMessage` | 20 | Same receipt rule |
| `EDIT_CAPTION` | `editMessageCaption` | 20 | `bool(decoded.get('ok'))` |
| `EDIT_TEXT` | `editMessageText` | 20 | Same Boolean rule |
| `LEGACY_PHOTO` | `sendPhoto` | 40 | Any decoded JSON yields `True`, even `ok=false` |
| `LEGACY_TEXT` | `sendMessage` | 15 | Decoded JSON discarded, normal return `None` |
| `BOT_TEXT` | `sendMessage` | 30 | Full decoded JSON |
| `BOT_PHOTO_BYTES` | `sendPhoto` | 30 | Full decoded JSON |

Only the two bot wrappers omit HTML. Only the bytes bot wrapper adds a reply ID;
that field is distinct from a topic. HTTPX receives the direct bytes value as
proven; pinned HTTPX chooses multipart filename `upload` and MIME
`application/octet-stream`. The report did not recover Requests' emitted wire
headers for that raw-byte form, so wire equality for those defaults is unproven.

The original one-destination threaded caller can forward through the important
bot only after the primary selector returns normally. The receipt-returning
caller has no important-bot path. Neither behavior is wired to the new client.
The receipt caller guards falsey configured token/destination before applying
inactivity state. The pure helpers are components, not a complete fanout engine;
callers must retain those ordering distinctions if they later reconstruct it.

## Manual desktop commands

The **Уведомления** tab loads `TG_BOT_API_KEY`, `TG_BOT_CHAT_ID`, and
`PROXY_FOR_TG` only after the operator presses the load button. Token and proxy
fields are masked; editing this form does not save settings. A comma-separated
legacy destination must be reduced explicitly to one `chat_id[:thread_id]`.
The panel offers text, one photo with caption, and editing by message ID.
Text/captions use HTML. Automatic event delivery and batch fanout are not wired.

Each new command checks the existing online lease before constructing a client.
The simulator refuses real sends even if the submit method is called directly.
Cancellation and application shutdown need no lease. Each command owns a fresh
client, keeps the form busy until request/cleanup completion, and sends at most
one HTTP request. Closing the window cancels and joins its request; cancelling a
close waiter does not detach cleanup. A confirmed result survives cancellation
during client closure. An uncertain result tells the operator to inspect the
chat before submitting again. No durable notification queue or automatic replay
exists in this version.

The imported proxy is retained verbatim. Unsupported legacy syntax or a malformed
value fails before transmission instead of silently selecting a direct connection.
Use the conventional HTTP(S) URL format described above. `PROXY_FOR_TG` is now
redacted in ordinary settings exports and sealed by the application settings
vault, including case variants. The v1 application vault remains readable under
its original sensitivity rules; loading it is read-only. The next save upgrades
to v2 and creates a sealed v2 backup of the previous settings. A v2 envelope
rejects unprotected sensitive fields. Version labels are compatibility metadata,
not authenticated downgrade protection. Previously retained external plaintext
source files and backup copies are not rewritten by this change.

These UI workflows are tested with synthetic MockTransport requests only. They
are new operator controls over recovered send/edit contracts, not proof of the
original screen layout or live Telegram delivery.

## Evidence and verification limits

The implementation handoff is the sanitized static report
`TELEGRAM_SPEC_SANITIZED.md`, checkpoint 2026-10-15, SHA-256
`4743a18227969065aa9537718dbe8b13560f275ae656786023d8ef0a8178a349`.
The target is `Panel.exe` SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
Raw decompilation, constants, credentials and private evidence are not published.

| Evidence | Native addresses |
| --- | --- |
| Receipt senders; edits | `0x1406d28d0`, `0x1406d37f0`, `0x1406d42e0`, `0x1406d4e20` |
| Legacy senders and selector | `0x1406d9390`, `0x1406da3a0`, `0x1406d5d10` |
| One-destination, threaded and receipt callers | `0x1406cbc90`, `0x1406cdc00`, `0x1406d0460` |
| Proxy and important-message filter | `0x1406d6410`, `0x1406dabd0`; predicate `0x1405eff58`–`0x1405effcd` |
| Bot wrappers and item formatter/producer | `0x140715b30`, `0x140716200`, `0x140627380` |

Tests cover request/form/multipart contracts, legacy quirks, negative/malformed
responses, deadline after simulated remote effect, cancellation before/after
dispatch, repeated cancellation during response close, owned shutdown, file
cleanup, fixed origin/TLS/proxy settings, and scoped secret-safe logging.

These tests establish the reconstruction's behavior against the static contract.
They do not prove Telegram acceptance of repeated photo parts, live proxy/API
behavior, original/new runtime parity or actual delivery. No Telegram, Steam CDN
or original application was contacted. Upstream event templates, nickname/system
prefixes, omitted callable defaults, item icon lifecycle and full bot command
handling remain outside this slice. Icon downloads and automatic producer/fanout
integration remain open. [Offline image composition](item-composite.md) implements
the recovered two-image geometry and paste behavior through a separate API and
desktop subtab, without downloads or automatic delivery.
