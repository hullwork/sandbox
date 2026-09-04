# Deployment and validation

## GitOps and OCI Helm package

The product-owned chart in `charts/sandbox` is the portable deployment
contract. It depends on nothing outside this repository: `helm install` it
directly, or consume it from Argo CD, Flux, or another OCI-aware GitOps
controller. Release tags publish it to `oci://ghcr.io/hullwork/charts/sandbox`.

```bash
helm lint charts/sandbox
helm template sandbox charts/sandbox
```

`values.schema.json` validates the public configuration surface. Every product
image accepts an immutable `sha256:` digest; when set, the digest takes
precedence over its tag. What the chart needs from the cluster - a PostgreSQL or
MySQL database, S3-compatible object storage, an RWX StorageClass, and a gVisor
RuntimeClass - is configured through `values.yaml` and described in the
[Production guide](PRODUCTION.md).

The existing Kustomize bases remain supported for source deployments. The Helm
chart is the versioned package boundary for external GitOps composition.

This is a multi-namespace package. Override `namespaces.system` and
`namespaces.workloads` together; an Infra Stack destination namespace does not
replace these values. Release `package-metadata.json` follows the shared schema
and carries the OCI Chart digest plus all four runtime-image digests and their
Helm value paths.

## Local integration

Lima provides the Linux VM in the standalone integration environment; kubeadm
provides Kubernetes inside those VMs. Sandbox Platform itself talks to Kubernetes and
does not depend on the Lima API.

The standalone integration entrypoint creates one dedicated Lima VM, bootstraps a
kubeadm cluster, installs checksum-pinned Cilium and gVisor, loads locally built
images plus the pinned Metrics Server image into containerd, and applies a
self-contained development profile:

```bash
make quickstart
make status-local
make dev-token
make down-local
```

`make quickstart` runs `make doctor`, creates the repository-local `.venv`,
deploys this kubeadm profile, and executes a live gVisor/persistence/fail-closed
proof. It records phase timing and outcome in
`.sandbox/quickstart-summary.json`. Use `make up-local` directly when the Python
environment is already prepared and only the deployment needs updating.

For a new profile, `make doctor` fails when less than 8 GiB of memory or 35 GiB
of disk is free. When the dedicated `sandbox-local` VM already exists, it uses a
2 GiB memory / 5 GiB disk reuse gate instead. It
warns when `/dev/kvm` is absent on Linux (Lima then falls back to QEMU software
emulation, which is far slower). Set `SANDBOX_DOCTOR_SKIP_RESOURCES=1` to skip the
resource gate.

The cluster-admin kubeconfig is written to `.sandbox/kubeconfig`, never to
`~/.kube/config`. Make targets that talk to the cluster (`dev-token`,
`control-plane-forward`, `console-forward`) export that path as `KUBECONFIG` themselves;
for direct `kubectl` use, export it once:

```bash
export KUBECONFIG="$(scripts/local-cluster.sh kubeconfig)"
kubectl get nodes
```

Required host tools are Docker, Lima, kubectl, Helm, and OpenSSL. The default VM is
`sandbox-local`; `down-local` stops it without deleting its disk. Override the
name or state directory with `SANDBOX_LOCAL_VM` and `SANDBOX_STATE_DIR`.

Remove everything the profile created on the host, that is the Lima VM and its
disk, the `.sandbox` state directory including the kubeconfig, and the four locally
built images, with:

```bash
make destroy-local
```

The host API and Control Plane ports default to `18448` and `18080`. Override them when
creating a VM with `SANDBOX_LOCAL_API_PORT` and `SANDBOX_LOCAL_CONTROL_PLANE_PORT`. VM resources can likewise
be adjusted with `SANDBOX_LOCAL_CPUS`, `SANDBOX_LOCAL_MEMORY_GIB`, and `SANDBOX_LOCAL_DISK_GIB`:

```bash
SANDBOX_LOCAL_VM=sandbox-test \
SANDBOX_STATE_DIR="$PWD/.sandbox-test" \
SANDBOX_LOCAL_API_PORT=28448 \
SANDBOX_LOCAL_CONTROL_PLANE_PORT=28080 \
SANDBOX_LOCAL_CPUS=2 \
SANDBOX_LOCAL_MEMORY_GIB=4 \
make up-local
```

Port overrides belong to the VM at creation time. When reusing an existing VM,
the script rejects mismatched values instead of silently writing a broken
kubeconfig.

This profile proves the Lima/kubeadm/gVisor path, but uses single-node local-path
storage, SQLite, and an in-cluster Ceph RGW development profile backed by local disk.
Data survives Pod restarts but not deletion of the VM disk. It is not a
production durability or HA claim.

`scripts/local-cluster.sh` installs the Rook chart, applies
`rook/cluster-local.yaml`, waits for `CephCluster` and `CephObjectStore` readiness,
copies the generated `sandbox-runtime` user into `object-store-credentials`, and then
runs the versioned bucket-initialization Job. The endpoint is
`http://rook-ceph-rgw-object-store.rook-ceph.svc.cluster.local:80`; the buckets are
`user-uploads`, `agent-data`, and `sandbox-workspaces`.
Production operators should follow [Production](PRODUCTION.md), supply PostgreSQL,
durable object and RWX storage, and render the base manifests for their platform.
Official tag releases also attach `sandbox-vX.Y.Z.yaml`, with all four project
images pinned to the exact signed GHCR digests. Supply the
[pre-provisioned resources](#pre-provisioned-resources) before applying it.

## Environment profiles

| Environment | Kube context convention | Configuration source | Storage/database |
| --- | --- | --- | --- |
| Local | `sandbox-local` | `overlays/local`, generated local Secrets | SQLite, in-cluster Ceph RGW, local-path RWO Workspace storage |
| Development | `sandbox-dev` | Base plus operator GitOps values | PostgreSQL, external object store, RWX CSI |
| Staging | `sandbox-staging` | Base plus operator GitOps values | Production-equivalent managed dependencies |
| Production | `sandbox-prod` | Base plus reviewed provider adapter | Durable managed dependencies and backup |

ACK and EKS are platform implementations, not environment names. Use the neutral
base when the cluster exposes the required capability names; apply `overlays/eks`
only for its concrete StorageClass difference. See the
[platform capability contract](PLATFORM_CONTRACT.md).

`overlays/external-deps` is an opt-in example, referenced by no default path, for
running Control Plane against a managed MySQL instance and an external S3-compatible
object store. Every host, endpoint, and bucket in it is a placeholder, and it needs
two additional Secrets (`sandbox-mysql-auth`, `sandbox-oss-credentials`) that its
header comment shows how to create. It is experimental even though the default Control
Plane image includes the checksum-locked `PyMySQL` driver. PostgreSQL remains the
supported production backend; MySQL changes require the backend-specific contract tests
and an explicit compatibility review.

## Pre-provisioned resources

The base manifests reference these objects by name and never create them. The
local profile generates the Secrets with random values
(`scripts/bootstrap-local-secrets.sh`); every other environment must create them
before `kubectl apply`.

| Resource | Kind | Namespace | Keys or requirement |
| --- | --- | --- | --- |
| `sandbox-api-credentials` | Secret | `sandbox-system` | `control-plane-token`, `signing-key`, `workspace-id-key` |
| `sandbox-postgres-auth` | Secret | `sandbox-system` | `database`, `username`, `password` |
| `object-store-credentials` | Secret | `sandbox-system` | `access-key`, `secret-key` |
| `sandbox-volume-auth` | Secret | `sandbox-system` and `sandbox-workloads` | `token`; both copies must hold the same value |
| `sandbox-postgres` | Service (or ExternalName) | `sandbox-system` | Resolves to PostgreSQL on port 5432 |
| `object-store-config` | ConfigMap | `sandbox-system` | Patch `endpoint`, the three bucket names, and `health-path` for your provider; the managed Ceph endpoint is `http://rook-ceph-rgw-object-store.rook-ceph.svc.cluster.local:80` |
| `sandbox-rwx` | StorageClass | cluster | ReadWriteMany capable; `overlays/eks` substitutes the EFS class |
| `gvisor` | RuntimeClass handler | cluster | `runsc` installed on every node labeled `sandbox.hullwork.com/node-role=runtime` |

The Helm chart names the database Secret with `postgresql.authSecret` (default
`sandbox-postgres-auth`). That single value is used by the embedded PostgreSQL
StatefulSet, the Control Plane environment, and its password volume; overriding
it never requires patching an additional hard-coded reference.

```bash
kubectl -n sandbox-system create secret generic sandbox-api-credentials \
  --from-literal=control-plane-token="$(openssl rand -hex 32)" \
  --from-literal=signing-key="$(openssl rand -hex 32)" \
  --from-literal=workspace-id-key="$(openssl rand -hex 32)"
kubectl -n sandbox-system create secret generic sandbox-postgres-auth \
  --from-literal=database=sandbox \
  --from-literal=username=sandbox \
  --from-literal=password='<postgres-password>'
kubectl -n sandbox-system create secret generic object-store-credentials \
  --from-literal=access-key='<object-store-access-key>' \
  --from-literal=secret-key='<object-store-secret-key>'
VOLUME_TOKEN="$(openssl rand -hex 32)"
for namespace in sandbox-system sandbox-workloads; do
  kubectl -n "$namespace" create secret generic sandbox-volume-auth \
    --from-literal=token="$VOLUME_TOKEN"
done
kubectl -n sandbox-system create service externalname sandbox-postgres \
  --external-name=postgres.example.internal
```

`workspace-id-key` derives stable workspace IDs; changing it changes every derived
ID, so treat it as non-rotating (see [Configuration](CONFIGURATION.md)).

The base manifests pin the four project images to fixed local tags with
`imagePullPolicy: Never`, which only works when the images were imported into the
node's container runtime, as the Lima profile does. On a real cluster do not apply
`k8s/` or the overlays directly: start from the release manifest
`sandbox-vX.Y.Z.yaml` (registry-qualified, digest-pinned images) and layer your
storage and endpoint patches on top of it.

## Console

```bash
npm --prefix console ci --ignore-scripts
npm --prefix console run test:i18n
npm --prefix console run lint
npm --prefix console run typecheck
npm --prefix console run build
```

The console is deployed as static files and does not receive Control Plane admin credentials.
Its Service uses NodePort `30081` (Control Plane keeps `30080`). The Overview polls the
authenticated `/v1/monitoring` snapshot every five seconds and shows cluster nodes
only to global identities. The Lima profile installs the pinned local Metrics Server, so
live CPU and memory usage becomes available after its first scrape. Production/base
manifests deliberately do not install a cluster-wide Metrics API: operators must
supply one. When it is absent or temporarily unavailable, the Console explicitly
keeps health, capacity, resource requests/limits, and restart data while marking
actual usage unavailable.

## Full E2E

After deploying a cluster and exposing Control Plane, set the isolated kubeconfig explicitly
and provide a token through the environment; the runner must never read a global
current context by accident:

```bash
KUBECONFIG=/path/to/sandbox.kubeconfig \
SANDBOX_KUBE_CONTEXT=sandbox-local \
SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080 \
make e2e-local
```

The runner verifies network policy, core behavior, object storage, restart recovery, and adversarial paths. It requires both documented namespaces and a reachable Control Plane health endpoint.

## Validation boundary

`make test` is a real standalone unit/contract test gate. CI also validates the
console, wheel entrypoints, Kustomize rendering, shell syntax, and all image builds.
Cluster-facing changes require a live gVisor E2E. Record the cluster version,
RuntimeClass handler, storage class,
source commit, image digests, and exact checks executed.
