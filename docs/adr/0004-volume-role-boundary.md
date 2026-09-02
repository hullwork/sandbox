# ADR 0004: Separate Control Plane orchestration from mounted-volume access

- Status: accepted

## Context

Control Plane runs in `sandbox-system`, while Workspace PVCs live in
`sandbox-workloads`. Giving Control Plane every Workspace mount would expand its storage
blast radius and couple the control plane to workload volume topology.

## Decision

Control Plane owns lifecycle and authorization. A separately deployed volume role performs
the bounded offline file and Workspace operations that require mounted storage.
Runtime serves operations that require an active sandbox.

## Consequences

Some file operations remain available while Runtime is offline; complex operations
return an explicit conflict until Runtime exists. Control Plane and volume-role credentials,
RBAC, and NetworkPolicy must remain separate.

## Verification

Manifest review must confirm namespace/RBAC separation. File-service tests and live
E2E must cover path traversal, cross-owner denial, and offline-operation boundaries.
