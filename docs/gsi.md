# Local game-state telemetry

Use `GameStateReceiver` to observe the latest GSI payload for an explicitly registered
account. It runs without a license lease because receiving telemetry is a viewing operation.
It has no game-action, process, configuration-file, database, or network-client adapter.
Game launch, lobby behavior, farming, and their state transitions remain unavailable.

The GUI does not start this listener. A future real `GameAdapter` must deliberately register
accounts and own the listener lifetime. No original game or Steam files are written.

## Start an owned listener

```python
import asyncio
import secrets

import httpx

from fsm_panel.gsi import GameStateReceiver, GsiServer


async def main() -> None:
    receiver = GameStateReceiver()
    token = secrets.token_urlsafe(32)
    receiver.register("synthetic-account", "76561198000000001", token)

    async with GsiServer(receiver) as server:
        # An ephemeral port is selected. Hand server.url and token privately to
        # an authorized future game adapter; do not log or persist the token.
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(server.url, json={
                "provider": {"steamid": "76561198000000001"},
                "auth": {"token": token},
                "map": {"phase": "warmup"},
            })
            assert response.status_code == 200
        snapshot = receiver.snapshot("synthetic-account")
        assert snapshot is not None
        assert "auth" not in snapshot.payload
        assert snapshot.payload["map"]["phase"] == "warmup"

    receiver.unregister("synthetic-account")


asyncio.run(main())
```

Construction opens no socket. `await server.start()` and `await server.close()` are also
available and idempotent. The listener binds only the literal IPv4 address `127.0.0.1`.
`port=0` chooses an ephemeral port; an explicit port must be between 1 and 65535.
A bind conflict raises `RuntimeError("gsi_bind_failed")`; there is no fallback address.
Closing stops reception but retains memory snapshots until unregistration or receiver disposal.
The server does not install process signal handlers or trust forwarding headers.

## Registry and snapshots

| API | Behavior |
| --- | --- |
| `register(account_id, steam_id, token)` | Associate a unique account and Steam ID with a unique token. At most 50 registrations. |
| `unregister(account_id)` | Revoke reception and discard the snapshot; safe to repeat. |
| `snapshot(account_id)` | Return an independent `GameStateSnapshot`, or `None` before reception or after unregistration. |
| `receive(body: bytes)` | Parse, authenticate, redact, and retain one complete UTF-8 JSON payload; return an independent snapshot. |

Account IDs contain 1–128 characters. Steam IDs are nonzero canonical decimal strings within
unsigned 64-bit range. Numeric JSON IDs, signs, whitespace, and leading zeroes are rejected.
Tokens contain 32–256 printable ASCII characters without spaces. Generate them with
`secrets.token_urlsafe(32)`; length validation alone cannot establish randomness.
Only SHA-256 token digests are retained in the registry, compared with `hmac.compare_digest`.
Reusing a token across accounts is rejected. A valid token for one account cannot authenticate
another registered Steam ID.

Duplicate registration raises a stable `GsiError`. To rotate credentials, unregister and then
register the new token. Old tokens stop working immediately. Registration has no automatic
expiry; the owner must unregister an account when telemetry is no longer needed.

Snapshots contain `account_id`, `steam_id`, `received_at`, and `payload`. The timestamp uses
the injected core `Clock.monotonic()` when an authenticated arrival is stored. It is process-local
time, not an epoch timestamp. The last accepted arrival replaces the whole previous payload.
Provider timestamps do not order arrivals. No stale-payload, replay, or expiry rule is inferred
from the original handlers. Consumers can show elapsed observation age without treating it as
permission to start or change a game.

Returned payloads are deep copies. Dictionary keys named `auth` or `token`, ignoring case,
are removed at every depth before storage. The module does not log requests, tokens, payloads,
or upstream exception text, and it never creates an error-dump file. Snapshot representations
exclude the payload. Other telemetry values remain untrusted data and must not be logged wholesale.

## HTTP contract and bounds

The request shape is in [gsi-payload.schema.json](../contracts/gsi-payload.schema.json).
Schema validation alone does not enforce byte, depth, identity, or token-association limits.

`POST /` returns an empty HTTP 200 response with `Content-Type: text/html; charset=utf-8`.
No telemetry-read HTTP endpoint is exposed. `create_gsi_app(receiver)` is available for ASGI
composition and tests; use `GsiServer` for the enforced network-binding boundary. Direct ASGI
hosts are responsible for their own trusted peer address and loopback binding.

The default body limit is 262,144 bytes, including authentication. The default nesting limit
is 32 JSON containers, counting the root. Receiver options can lower or adjust these bounds;
nesting is capped at 64 and registrations at 50. Nesting is checked before JSON parsing.
Duplicate JSON object keys, malformed UTF-8/JSON, and non-finite numeric telemetry are rejected.
Both declared and actual streamed body sizes are checked. Chunked bodies are supported.
Compressed requests and conflicting framing headers are rejected. The body deadline defaults
to five seconds and is configurable from greater than zero to 60 seconds.

| Status | Stable error codes |
| --- | --- |
| 400 | `invalid_payload`, `invalid_steam_id`, `invalid_content_length`, `client_disconnected` |
| 401 | `unauthorized` for missing/bad tokens or unknown Steam IDs |
| 403 | `loopback_required` |
| 408 | `body_timeout` |
| 413 | `payload_too_large`, `payload_too_deep` |
| 415 | `unsupported_content_encoding` |
| 503 | `clock_unavailable` |

Errors have the JSON shape `{"error": "stable_code"}` and never echo input.
Failed requests leave the latest accepted snapshot unchanged. Registry-specific error codes
include `registration_exists`, `token_already_registered`, and `client_limit_reached`.

## Evidence and deliberate differences

The source is the private sanitized `research-private/native/portable-sanitized/STATIC_FINDINGS.md`
report for Panel.exe SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
Manual bounded Ghidra decompilation completed for two native handlers. This is static evidence;
it is not successful native REA analysis, original source recovery, or runtime parity.

| Recovered handler behavior | This implementation |
| --- | --- |
| `do_POST`, native `0x1409ffad0`, source line 26986: read integer Content-Length bytes, decode UTF-8, parse JSON, call payload helper. | Decode UTF-8 JSON after explicit byte/depth bounds; bounded streamed bodies are also accepted. |
| After the helper returns, send HTTP 200, `Content-Type: text/html`, then end headers. | Accepted requests return empty 200 text/html. New validation failures receive explicit errors. |
| `get_info_about_players`, native `0x1409ff130`, source line 26970: convert `provider.steamid` to integer for membership in `LAUNCHED_ACCOUNTS_IDS`. | Match canonical string IDs to an explicit account registry and verify the associated token. Registration does not assert that a game was launched. |
| Store the whole payload in `GAME_INFO`, keyed by the original Steam ID value. Nonmembers return normally. | Keep the latest auth-redacted payload and original canonical Steam ID under a stable account ID. Reject nonmembers with the same 401 as a wrong token. |
| Helper exception path references an error-dump helper and `payload.json`. | Return only fixed error codes; write no payload dumps. |

Loopback binding, token authentication, token uniqueness, redaction, limits, arrival timestamps,
explicit shutdown, and registration lifecycle are new defensive design decisions. The recovered
handlers establish none of those policies. Their surrounding listener allocation and lifetime,
launch orchestration, field-to-UI mapping, and game actions remain unresolved.

## Verification

Run `python -m pytest tests/gsi`. Tests cover real localhost HTTP reception and closure,
ASGI errors and stream bounds, 50 isolated account snapshots, cross-account spoof rejection,
token revocation, nested redaction, independent snapshots, clock failures, and observed arrival
order. Synthetic fixtures contact no Steam service and do not execute the original application.
These tests validate the new receiver contract only.
