# ADR 0002: Separate readiness from dependency health

- Status: accepted

## Context

Control Plane depends on Kubernetes, state storage, and object storage. Marking a healthy
Control Plane replica unready for every downstream outage can remove all Service endpoints
and turn a partial dependency failure into total control-plane unavailability.

## Decision

`/readyz` reports whether this process should receive traffic, including shutdown
state. `/healthz` reports stricter downstream health for operators and deployment
validation. `/livez` remains a process liveness probe.

## Consequences

A ready Control Plane may return route-specific `503` responses when a dependency is down.
Monitoring must evaluate both process readiness and dependency health.

## Verification

Kubernetes probes must use the documented endpoints. Tests must preserve the
different shutdown and dependency-failure semantics.
