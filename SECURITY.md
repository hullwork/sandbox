# Security policy

## Supported versions

Security fixes target the latest published release and current `main`. Historical source snapshots are not independently supported.

## Reporting a vulnerability

Use the repository's GitHub private vulnerability reporting form. Do not open a public issue for vulnerabilities.

If the form is unavailable, open a public issue containing no vulnerability
details and ask the maintainers to establish a private reporting channel.

Include the commit, affected component, impact on workspace isolation or credential scope, reproduction steps, and logs with secrets removed. Maintainers aim to acknowledge reports within five business days.

## Security boundary

- Sandbox Platform is an alpha execution control plane for controlled environments, not a public multi-tenant shell service.
- Agent-facing shell and file operations execute through Runtime MCP inside gVisor. Control Plane failure must not fall back to host execution.
- Runtime Pods must remain non-root, read-only at the root, without Kubernetes service-account credentials, and subject to the documented egress policy.
- Workspace ownership and scoped tokens prevent one workspace from reading or mutating another.
- Control Plane admin credentials are separate from tenant and runtime credentials and must never be exposed as agent identity.
- Object access uses separately scoped credentials and owner-partitioned keys;
  Control Plane service accounts must not hold Ceph administrative API
  capabilities or create buckets beyond the configured quota.
- Checkpoint restore rejects path traversal, links, devices, oversized archives, and unexpected archive structure.
- The local topology exercises gVisor but does not provide durable PostgreSQL,
  multi-node storage, or production high availability.

## Operator requirements

- Confirm gVisor is enabled on Runtime nodes before scheduling workloads.
- Enforce the runtime node label, Kubernetes NetworkPolicies, storage classes, and workload quotas.
- Store Control Plane tokens outside the workspace and rotate admin credentials independently.
- Review object-store credentials, bucket versioning, and retention before enabling checkpoint recovery or purge operations.

The detailed assets, threats, assumptions, and verification checklist are in
[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).
