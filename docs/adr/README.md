# Architecture decision records

Architecture decision records capture constraints that remain important after the
implementation work or incident that introduced them. They are not a chronological
change log; use [CHANGELOG.md](../../CHANGELOG.md) for user-visible changes.

| ADR | Decision |
| --- | --- |
| [0001](0001-fail-closed-execution.md) | Agent execution fails closed and never falls back to the host |
| [0002](0002-readiness-and-dependency-health.md) | Readiness represents whether this Control Plane replica should receive traffic |
| [0003](0003-stable-workspace-identities.md) | Workspace identity derivation uses a stable key independent of token signing |
| [0004](0004-volume-role-boundary.md) | Control Plane orchestrates storage through a separate volume role |
| [0005](0005-sandbox-capability-tickets.md) | Sandboxes verify capability tickets with a per-instance key, never the signing key and never by asking the control plane |

New records should state context, decision, consequences, and verification. Supersede
old records rather than silently rewriting decisions that shipped.
