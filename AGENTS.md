# Project operating rules

Use this repository's approved FSM_PANEL_REBUILD_PLAN.md as the project charter.
Delegate independent work to the ten configured roles; at most three children plus coordinator.
Use gpt-6-astra, effort max for analysis/review and xhigh for ordinary implementation.
Keep original FSM_PANEL files immutable. Never commit credentials, real account fixtures,
private native dumps, private keys, or runtime databases. Record evidence versus inference.
Every code change needs relevant behavioral tests. Never claim simulator tests prove legacy
parity. Undiscovered game behavior stays unavailable, with an explicit reason in UI and docs.
Use separate worktrees and one owner per subsystem. Coordinate public contract changes.
The native-analysis agent exclusively owns the REA active target. Do not change its session.
GUI does no business logic; model changes are delivered through Qt signals on the UI thread.
Real Steam actions and original-app execution happen only in explicitly assigned stand runs.
Licensing has no payment code. Stop/export/recovery operations remain available without a lease.
