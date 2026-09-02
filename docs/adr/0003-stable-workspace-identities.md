# ADR 0003: Keep workspace identity stable across signing-key rotation

- Status: accepted

## Context

Workspace IDs are deterministically derived from caller identity. Reusing the
short-lived access-token signing key for that derivation would make normal key
rotation change every derived Workspace ID and orphan existing data.

## Decision

`WORKSPACE_ID_KEY` is a separate, stable recovery asset. `SIGNING_KEY` may rotate
according to the credential plan; `WORKSPACE_ID_KEY` must be backed up with database,
workspace, and object-store state and is not rotated during normal releases.

## Consequences

The stable key has a long security lifetime and requires stronger backup controls.
Losing or changing it breaks deterministic access to existing Workspaces.

## Verification

Upgrade, rollback, and disaster-recovery procedures must preserve the key and verify
that known identities resolve to their existing Workspace IDs.
