# Architecture and trust boundaries

The desktop uses a Qt main thread and a separate asyncio coordinator thread. UI sends
commands; immutable events return through Qt signals. Account operations are serialized.
Repositories and adapters own storage, processes, windows, secrets and external services.
The pure core does not import Qt, Steam libraries or payment code.

The production composition uses a fail-closed license admission gate and an explicit
unavailable game adapter until native behavior is recovered. The simulator must be
selected explicitly and is visibly labelled. It is never evidence of real game parity.

The entitlement service receives access rights from an external application over a
separately authenticated internal API. The desktop does not process payments. Short
Ed25519-signed leases bind to devices; monotonic expiry prevents extending validity
by moving the local wall clock. Stop, export and reconciliation do not need a license.

Steam jobs use versioned JSON Lines over inherited private pipes, not secret-bearing
command lines. An external-operation journal must persist send intent before sending.
Lost responses are `unknown_outcome`; retry cannot silently send another offer.

Original architecture and new design are deliberately documented separately. The legacy
source establishes an event/callback looter, but does not establish Panel.exe's module graph.
