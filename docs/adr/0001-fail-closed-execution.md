# ADR 0001: Fail-closed agent execution

- Status: accepted

## Context

An agent may execute untrusted commands. Falling back to the agent host when Control Plane,
Runtime, or the network is unavailable would silently remove the isolation boundary.

## Decision

SDK, CLI, and MCP operations surface Control Plane or Runtime failure to the caller. They
never execute a command or access a workspace through the local host filesystem as a
fallback.

## Consequences

Availability failures remain visible and may interrupt an agent run. This is safer
than producing a successful result outside gVisor. Callers may retry bounded,
idempotent operations but must not substitute local execution.

## Verification

Tests and reviews must reject local `subprocess` or filesystem fallback in the client
surface. Live E2E must confirm that commands run in the expected RuntimeClass.
