# Production readiness guide

The repository is alpha. A production deployment is an operator-built Kubernetes
installation, not the local integration profile.

## Required platform capabilities

- Kubernetes with a CNI that enforces NetworkPolicy.
- Kubernetes Metrics API (`metrics.k8s.io/v1beta1`) when the Console must show live
  node and per-Runtime CPU/memory usage. Use the platform service or a securely
  configured Metrics Server with trusted kubelet serving certificates; never copy
  the local overlay's `--kubelet-insecure-tls` setting into production.
- gVisor installed and exercised on dedicated, labelled Runtime nodes.
- Durable RWX storage for the shared workspace PVC, with verified capacity and backup.
- Durable PostgreSQL and an independently operated S3-compatible object store with TLS, versioning/retention,
  backup, and least-privilege credentials.
- External secret management, authenticated TLS ingress, DNS, monitoring, audit-log
  retention, and image registry access.

The exact Kubernetes objects an operator must create before applying the base
manifests (namespaces, StorageClass, RWX PVC, Secrets, RuntimeClass, and the
Metrics API) are listed in the pre-provisioned resources table of
[Deployment and validation](DEPLOYMENT.md).

Lima is the VM provider used by the standalone local integration environment; the
cluster inside it is kubeadm Kubernetes. Lima is not a Sandbox Platform runtime
dependency. Other Kubernetes infrastructure is valid if it satisfies the same
contract. The production/base render intentionally does not own the cluster-wide
Metrics API or its `APIService`.

## Control Plane is a single-replica control plane

`k8s/control-plane.yaml` runs one Control Plane replica with `strategy: Recreate`. Every release
therefore includes a short outage: the old Pod is terminated (it reports `503` on
readiness, drains in-flight requests for up to the configured shutdown budget, then
exits) before the new Pod starts. During that window:

- New `sandbox create`, SDK, and MCP calls fail with a Control Plane-unavailable error and
  can be retried once `/readyz` returns `200`.
- Runtime Pods keep running; `exec` calls that were in flight through Control Plane fail and
  can be retried. Workspace files and PTY sessions inside the Runtime are unaffected.

Running more than one Control Plane replica is currently unsafe and unsupported. Runtime
admission counting, in-flight creation slots, object-client slots, and the reaper are
process-local state; two replicas would overshoot `SANDBOX_MAX_RUNTIMES`, race on
reaping, and return `404` for a Runtime that another replica is still creating. Do
not change `replicas` or the update strategy until those are moved into the store
and the reaper has leader election.

## Release gate

Before promotion, require unit/contract tests, console checks, clean wheel install,
all image builds, rendered-manifest review, secret scanning, SBOM/provenance, image
vulnerability scanning, and a live gVisor E2E covering shell, file isolation, object
tickets, checkpoint restore, restart recovery, and cross-owner denial.

Every access log line carries `trace_id`, and the same value is returned to the
caller as `X-Request-Id`; a report that quotes it can be resolved to the exact
request. Where a tracing-aware gateway or mesh sits in front of Control Plane, its
`traceparent` is adopted, so these ids join that trace rather than starting a
parallel one.

Monitor Control Plane availability, admission failures, Runtime create latency, reaper and
reconciliation errors, credential usage (especially
`sandbox_credential_uses_total{kind="break-glass"}`), object-store
errors, database availability, PVC capacity, node pressure, and Runtime hard-TTL
deletions. Alerting thresholds depend on the operator's capacity and SLO.

The built-in Console is a current-state operational view, not a historical metrics
store or alerting system. Retain Prometheus-compatible telemetry externally for
history and alerts. Control Plane's monitoring identity has read-only access to Nodes and
Metrics API resources; do not add lifecycle mutation verbs to that ClusterRole.

## Upgrade and rollback

Pin images by immutable digest. Upgrade the control plane first only when the target
Runtime/API versions are declared compatible. Back up durable state, render the exact
overlay, canary one deployment, run the live acceptance suite, then promote. Rollback
must restore a compatible application/database pair.

`WORKSPACE_ID_KEY` is required, non-rotating, and must be backed up together with the
database, the workspace PVC, and the object store as one recovery set. Workspace
identifiers are derived from it; restoring a database or volume with a different key
produces new, empty Workspaces and orphans the old directories. Never rotate it as
part of an upgrade or rollback. `SIGNING_KEY` is the opposite: it signs only
short-lived tokens and tickets and may be replaced when leaked, at the cost of
invalidating in-flight tokens. See [Lifecycle and data](LIFECYCLE_AND_DATA.md) for
the restore order.
