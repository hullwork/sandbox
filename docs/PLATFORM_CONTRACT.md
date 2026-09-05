# Platform capability contract

Sandbox Platform depends on Kubernetes capabilities, not a specific cloud or
cluster product. A deployment is supported when each capability below has one
selected implementation and passes the repository acceptance suite.

| Capability | Stable interface | Local implementation | Production responsibility |
| --- | --- | --- | --- |
| Workload network | Kubernetes `NetworkPolicy` | Cilium | Any enforcing CNI; add an overlay only for provider-specific probe or egress policy |
| Runtime isolation | Kubernetes `RuntimeClass` | gVisor `runsc` | Install and verify the configured handler on every eligible Runtime node |
| Control state | `SANDBOX_STORE_BACKEND` contract | SQLite | PostgreSQL with durable storage, backup, and schema-compatible rollback |
| Object storage | S3-compatible endpoint and `OBJECT_STORE_*` credentials | Cluster-local Ceph RGW development profile | TLS-enabled S3-compatible service with versioning, retention, least-privilege credentials, and tested restore behavior |
| Workspace files | Kubernetes PVC and CSI `StorageClass` | CephFS RWX claim | Shared RWX claim, verified capacity, snapshots/backup, and restore testing |
| High-performance disk | CSI `StorageClass` selected by overlay | Not claimed | Provider-specific class that satisfies the selected access mode and failure-domain contract |
| Secrets | Kubernetes `Secret` references | Random local bootstrap values | External secret manager and audited rotation |
| Elastic Runtime capacity | Kubernetes node labels, taints, and `RuntimeClass` | `sandbox-local-w1…wN`, managed by `make scale-workers` | A node-pool controller that verifies Node Ready, CNI, runtime handler, and workload schedulability |

## Stable and elastic nodes

When an elastic pool is enabled, system services and Runtime workloads have
separate scheduling contracts. Stable nodes host the Control Plane, Volume
service, database, object storage, ingress, and observability components. Elastic
workers host only disposable Runtime Pods and use the label
`sandbox.hullwork.com/node-role=runtime` plus the taint
`sandbox.hullwork.com/node-role=runtime:NoSchedule`.

The Helm values `scheduling.system.*` and `runtime.*` are the public integration
surface. An infrastructure project may populate those ordinary values, but the
Sandbox does not call an infrastructure repository or depend on its environment
variables. Both values default to empty so the standalone chart works on an
unlabelled Kubernetes cluster. The local installer supplies the label and taint
for every Runtime worker. A production node-pool adapter must set
both sides of the scheduling contract. Scaling a Runtime pool to zero is permitted
only after active Runtime Pods have drained; workspace durability comes from the
PVC/object-store contract, not from a worker's local filesystem. The local adapter
preflights all scale-down targets, cordons and rechecks them as a set before
stopping any of them, updates Runtime
admission and namespace quota with the worker count, and keeps a new node cordoned
until its Runtime image is available. At zero, new Runtime admission fails fast;
system and Workspace access remain online.

A worker is usable only after the node is Ready, the CNI is healthy, the selected
`RuntimeClass` handler works, and a probe Pod can be scheduled and become Ready.
This end-to-end convergence time is the node scale-up cold-start SLI. VM boot time
or Kubernetes Node registration alone is not sufficient.

## Provider adapters

The base manifests contain no cloud provider, fixed storage endpoint, node IP, or
bundled provisioner. Provider adapters may patch only platform facts such as a
StorageClass name, ingress annotations, or workload identity. They must not fork
Control Plane, Runtime, SDK, or API behavior.

- `overlays/local` is the only repository-managed integration environment.
- `overlays/eks` selects the EFS CSI StorageClass expected by that deployment.
- ACK and other conforming clusters use the base directly when they expose the
  neutral `sandbox-rwx` StorageClass. Add a provider overlay only when a real
  manifest difference exists.

Cluster names are environment names (`sandbox-local`, `sandbox-dev`,
`sandbox-staging`, `sandbox-prod`), not provider names. Operators may keep
provider-specific details in their kubeconfig context; the project receives the
exact target only through `SANDBOX_KUBE_CONTEXT`.

## Storage constraints

The portable topology uses one shared claim and mounts a workspace-specific
`subPath` into each Runtime. The multi-node local profile supplies CephFS RWX;
multi-node production likewise requires RWX. Requested PVC capacity is not proof of
hard quota enforcement.

Optional per-workspace PVC mode is not portable by StorageClass substitution alone:
the volume service must also enumerate and purge all workspace claims. A provider
must ship and test that complete topology before enabling the mode.

## Acceptance

Every adapter must pass Kustomize rendering, policy checks, image startup, and the
full cluster E2E suite. Production promotion additionally requires backup/restore,
node failure, storage failure, and database rollback evidence.
