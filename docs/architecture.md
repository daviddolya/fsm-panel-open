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

Local maintenance uses the same Qt-to-asyncio bridge. Video proposals contain
immutable file snapshots; apply runs off the UI thread, revalidates both inputs,
retains a backup and replaces only the panel's working template. Shutdown joins
in-flight file work before stopping the bridge.

`CpuLimitRuntime` adds lease/state admission and a serialized CPU command queue.
Its `CpuManagedGame` decorator attempts addressed BES cleanup before game stop
and recovery. BES instances have a separate durable journal and must never enter
the generic process termination list. A failed BES cleanup remains actionable
even if target cleanup succeeds; no command acknowledgement proves throttling.

`launch_plan` is pure preparation data. Its injected environment, existence
facts, timestamp and random suffix make the recovered transformations testable
without calling Steam, a shell, registry APIs or the filesystem. The proposal
CLI performs only explicit input/output file access; it has no executor.

`account_config` and `autoexec` separate pure legacy plans from guarded offline
workspace writes. Logical Steam/game paths never choose a write destination.
Bounded source and tree snapshots are rechecked before commit; directory staging
preserves unrelated files and retains the previous tree as a backup. Two renames
are not an atomic directory exchange, so power-loss recovery is an explicit limit.
The shared template package verifies its generic recovered constants by hash;
the frozen desktop smoke also loads that package to check resource delivery.

Original architecture and new design are deliberately documented separately. The legacy
source establishes an event/callback looter, but does not establish Panel.exe's module graph.
