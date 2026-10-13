# Implementation status

The accepted plan is being implemented. This is not yet a functional replacement for FSM_PANEL.
The original app is license-blocked, and its native game behavior has not been fully recovered.

## Evidence and constraints

- Original SHA-256: `3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
- 5,406 files; 137,624,045 bytes. 49 setting keys and types inventoried without exposing values.
- Looter dependencies differ from lock; no claim of reproducibility of the legacy npm tree.
- Native Ghidra/REA investigation is running separately; private evidence stays outside this repository.
- Original execution, live Steam/trades, 50 real clients, and the 24-hour stand run have not occurred.
- Local Docker daemon is unavailable; PostgreSQL concurrency results must come from actual PostgreSQL runs.
- `docs/feature-matrix.json` distinguishes schema/source evidence from unverified game behavior.

## Release gate

Do not label this build as parity-complete. Simulator and unit test passes are development
evidence only. Features blocked on original behavior must remain visibly unavailable.
Use the final verification report for actual executed checks and remaining blockers.
