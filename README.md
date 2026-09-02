# Sandbox Platform

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Run an agent's shell and file operations inside a gVisor Pod on your own
Kubernetes cluster.** A Control Plane owns Workspaces, quotas and credentials.
Agents reach it through a Python SDK, the `sandbox` CLI, or a stdio MCP bridge —
and through nothing else. If the Control Plane or the Runtime is unreachable the
operation fails; it never falls back to running on the host.

```text
 ┌─ your process ──────────────────────────────────────────────────────────┐
 │  Python SDK              sandbox CLI            stdio MCP bridge        │
 │  sandbox_platform/       `sandbox …`            for an agent runtime    │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │  HTTPS + tenant API key
                                │  the tenant is decided by the credential,
                                │  never by anything in the request body
════════════════════════════════▼═════════════════════════════════════════════
 namespace: sandbox-system                              trusted control plane
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  Control Plane  (control_plane/)                                        │
 │    admission · quotas · tokens · workspace and runtime lifecycle        │
 │      │                                                                  │
 │      ├── state ──────────► PostgreSQL · MySQL · SQLite                  │
 │      └── checkpoints ────► S3-compatible object store                   │
 │                            workspace archives only: no process state,   │
 │                            no memory, no writable container layer       │
 │                                                                         │
 │  Operator Console  (console/)   static; holds no credential of its own  │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │  in-cluster, NetworkPolicy-scoped
════════════════════════════════▼═════════════════════════════════════════════
 namespace: sandbox-workloads                default-deny, untrusted workload
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  Runtime Pod   RuntimeClass: gvisor                                     │
 │    runtime/       shell, PTY sessions, SSE streaming                    │
 │    file-service/  the one path that writes workspace files              │
 │       │                                                                 │
 │       └── /workspace ──► Workspace PVC ◄── Volume Agent (RWX placement) │
 └─────────────────────────────────────────────────────────────────────────┘

 If the Control Plane or the Runtime is unreachable the operation fails.
 It never falls back to running on the host.
```

## Try it

```bash
python3 -m venv .venv                  # a system-wide install is an error on Debian,
.venv/bin/pip install -e '.[test]'     # Ubuntu and Fedora (PEP 668)
make test                      # 831 unit and contract tests, no network, no cluster
make help                      # every Make target with its one-line description
```

That is the contract suite: no cluster, no credentials, nothing to clean up.
The virtual environment is not optional on Debian, Ubuntu or Fedora — a
system-wide `pip install` is refused there (PEP 668). `make test` uses
`.venv/bin/python` when it exists and `python3` otherwise, so there is no
activation step.
[Run the full local cluster](#run-the-full-local-cluster) when you want a real
gVisor Runtime.

**Status: alpha (`0.1.0`).** `main` is the only channel and not a stability
promise — [Known limitations](#known-limitations) is deliberately specific.
**Self-contained.** This repository is the whole product: it depends on no other
repository, and other products integrate with it as an external tenant through the
API keys and contracts documented here.

---

## Why this instead of the alternatives

| Compared with | What Sandbox Platform does differently |
| --- | --- |
| **Docker-in-Docker or a shared container** | Every Runtime is its own Kubernetes Pod under the `gvisor` RuntimeClass: non-root, read-only root filesystem, no service-account token, default-deny NetworkPolicy. No Docker socket is ever exposed to agent code. |
| **Hosted sandbox services** | Self-hosted on your cluster. Workspace files, checkpoints, credentials, and control-plane state stay with the operator. There is no vendor API in the request path. |
| **Firecracker-style microVM stacks** | Isolation comes from gVisor through the standard Kubernetes RuntimeClass, so any node with containerd and `runsc` qualifies — cluster nodes do not need KVM. |

Four things worth checking in the source before you spend more time:

- **Workspace lifetime is separate from Runtime lifetime.** Stopping a Runtime keeps
  its Workspace PVC and files. Checkpoints are an explicit recovery path, not the
  normal persistence path.
- **The runtime-driver abstraction refuses to pretend.** `SANDBOX_RUNTIME_DRIVER`
  accepts only `gvisor`; any other value makes the Control Plane exit at startup
  rather than silently emit a gVisor Pod under another name
  ([`control_plane/core.py`](control_plane/core.py)). There is no provider plug-in
  surface advertised that does not exist.
- **Tenant ownership is tested against a real database, not mocked away.**
  `require_workspace_tenant` returns `True` unconditionally when no store is
  configured, which would make an in-memory contract test useless.
  [`tests/test_api_authorization.py`](tests/test_api_authorization.py) therefore boots
  the Control Plane in a subprocess against SQLite, creates two tenants, and replays
  every by-id route with the wrong tenant's key — asserting a `404` arrives *before*
  any Kubernetes call, so ownership is proven to be checked ahead of the dependency.
- **Route authorization is a checked-in manifest, cross-verified three ways.**
  `ROUTE_AUTH` declares 60 routes; 53 require credentials and 7 do not
  (`/livez`, `/readyz`, `/healthz`, `/metrics`, and the three OIDC sign-in discovery
  endpoints, which a browser reaches precisely because it has no credential yet).
  [`tests/test_route_completeness.py`](tests/test_route_completeness.py) fails the
  build if the `api.py` dispatch table, the OpenAPI document, and `ROUTE_AUTH` ever
  disagree.

Measured performance on the local reference profile (Apple Silicon, dedicated Lima
VMs, 5 runs × 100 measured iterations, no warm Runtime pool):

| Operation | p50 | p95 |
| --- | ---: | ---: |
| gVisor Runtime cold start (new Pod, scheduling to ready) | 2.497 s | 2.789 s |
| Warm execution | 35.72 ms | 48.08 ms |
| Workspace create | 29.21 ms | 51.42 ms |

Full method, raw evidence layout, and the explicit statement of what this number is
*not* (it is not a cloud, multi-tenant, or node-cold measurement) are in the
[benchmark report](docs/BENCHMARK_REPORT_2026-09-01.md).

---

## Look at the surfaces

No Control Plane needed — each of these prints what it accepts:

```bash
sandbox --help          # create / run / exec / stop / list
sandboxctl --help       # operator surface: workspaces, templates, admin keys, audit
sandbox-mcp --help      # the nine agent-scoped MCP tools and their required env vars
make help               # every Make target with its one-line description
```

## Run the full local cluster

`make up-local` builds a single-node kubeadm Kubernetes cluster inside a Lima VM with
Cilium and gVisor, then deploys the whole platform into it. This is the only local
cluster profile the project ships.

### Prerequisites

`make doctor` checks every item below and **exits non-zero** if one is missing, so run
it first rather than discovering a gap halfway through the VM build.

| Requirement | Detail |
| --- | --- |
| Commands on `PATH` | `docker` (daemon reachable), `limactl`, `kubectl`, `helm`, `python3`, `openssl` |
| Python | 3.11 or newer |
| Host OS | macOS or Linux |
| Host architecture | amd64 or arm64 (`scripts/local-cluster.yaml` pins Ubuntu images for both; gVisor is installed for `x86_64` and `aarch64`) |
| **Available memory** | **8 GiB free** — a hard check, not a warning |
| **Free disk** | **40 GiB free** under `$LIMA_HOME` (default `~/.lima`) — also a hard check |
| Virtualization | On Linux, a readable and writable `/dev/kvm`. Without it Lima falls back to QEMU TCG software emulation, which boots kubeadm many times slower and is not usable in practice. `make doctor` warns rather than fails on this one. |
| Network | Egress to pull the Ubuntu cloud image, Kubernetes apt packages, Cilium, gVisor, Metrics Server, and Rook/Ceph images |

Set `SANDBOX_DOCTOR_SKIP_RESOURCES=1` to bypass only the memory and disk checks. The
VM itself uses 4 CPUs, 6 GiB memory, and a 60 GiB disk by default, adjustable through
`SANDBOX_LOCAL_CPUS`, `SANDBOX_LOCAL_MEMORY_GIB`, and `SANDBOX_LOCAL_DISK_GIB`.

### Bring it up

```bash
make doctor
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
make up-local
```

The first `make up-local` downloads and builds everything listed above, so its
duration is dominated by your network throughput; subsequent runs reuse the VM disk.
It ends by printing:

```text
Control Plane: http://127.0.0.1:18080
Kubeconfig: /path/to/checkout/.sandbox/kubeconfig
```

The kubeconfig is written to `.sandbox/kubeconfig` inside the checkout, not to
`~/.kube/config`, so the local cluster cannot collide with a context you already use.
The Makefile exports `KUBECONFIG` for its own targets, so `make dev-token`,
`make status-local`, and the port-forward targets need no manual export.

### First command

```bash
export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
sandbox run --name demo --stop -- python -c 'print("sandbox-ready")'
```

```text
sandbox-ready
```

The token is read through command substitution so its value never enters shell
history. The CLI passes the command's stdout and stderr through unchanged and exits
with the command's exit code. `--stop` releases the Runtime while preserving the named
Workspace `demo`.

### Tear it down

```bash
make down-local     # stop the Lima VM; its 60 GiB disk and .sandbox/ remain
make destroy-local  # delete the VM with its disk and the generated .sandbox/ state
```

`down-local` is the right choice between sessions — `up-local` reuses the stopped VM.
`destroy-local` is irreversible: Workspace files, checkpoints, and the SQLite state
inside the VM are gone. Images that `make images` built on the host stay in the local
Docker daemon until removed with `docker rmi`.

---

## Using it

Four surfaces reach the same Control Plane — a Python SDK, the `sandbox` CLI, a
stdio MCP bridge for agent runtimes, and the operator console. Each one is
documented with a worked example in [docs/USAGE.md](docs/USAGE.md), along with
the tasks that come up once something is running: issuing tokens, taking a
checkpoint, importing files, and reading the audit log.

## Architecture

The diagram at the top of this file shows the layout. This section is the
detail behind it.


| Component | Responsibility |
| --- | --- |
| [`control_plane/`](control_plane/) | HTTP API, policy, authentication, lifecycle orchestration, the provider-neutral Runtime Driver contract, and the built-in gVisor driver |
| [`runtime/`](runtime/) | Minimal shell MCP server: synchronous execution, SSE streaming, PTY sessions |
| [`file-service/`](file-service/) | Canonical Workspace file operations, embedded in the Runtime; mutating file requests require a running Runtime |
| [`k8s/`](k8s/) and [`overlays/`](overlays/) | Declarative Kubernetes resources for the reference deployment |
| [`charts/sandbox/`](charts/sandbox/) | Independently deployable Helm package |
| [`console/`](console/) | Static operator console; embeds no credential and keeps a user-provided key only in tab-scoped `sessionStorage` |
| [`sandbox_platform/`](sandbox_platform/) | The published Python package: SDK and `Sandbox` facade, `sandbox` user CLI, `sandboxctl` operator CLI, and the `sandbox-mcp` stdio bridge |

Two Kubernetes namespaces separate the trust levels: `sandbox-system` holds the
Control Plane and Console, `sandbox-workloads` holds Runtime Pods, the volume agent,
and the default-deny NetworkPolicies.

**What this project is not responsible for.** It does not provide accounts, regions,
VM snapshots, dynamic per-request firewall rules, brokered third-party credentials,
public-domain routing, or billing. Checkpoints are workspace archives in S3-compatible
storage: they do not capture processes, memory, or the writable container layer.
Node autoscaling, registry mirroring, and CSI behavior belong to the cluster operator.
See [System specifications](docs/SYSTEM_SPECIFICATIONS.md) for the full list.

### Request tracing

Every response carries `X-Request-Id`, and the same value appears in the Control Plane
access log as `trace_id`. The Control Plane adopts an inbound W3C `traceparent` when
there is one, so it joins a trace an upstream gateway already started rather than
beginning a parallel one; see the [HTTP and SDK contract](docs/API.md#request-tracing).

Known gap, stated here rather than left to be discovered: object-storage calls go out
through `boto3`, which does not yet propagate `traceparent`, so that hop is
not traced yet. Someone who finds a hole in a trace should be able to confirm it is
expected instead of first suspecting their own query.

---

## Deployment profiles

Kustomize overlays under [`overlays/`](overlays/) layer on the provider-neutral base in
[`k8s/`](k8s/). Every overlay renders with `kubectl kustomize <path>` except
`overlays/local-dev`, which is a Kustomize Component consumed by `overlays/local`
rather than a deployable overlay; CI checks the others on every push.

| Profile | Intended for | Maturity |
| --- | --- | --- |
| `overlays/local` | The `make up-local` VM. SQLite state, local-path PVCs, single pinned volume agent. | Reference. Exercises isolation and recovery behavior; its single-node storage and SQLite database are **not** a production durability claim. |
| `overlays/rwo-single-node` | Clusters without RWX storage. Trades volume-agent availability for portability: one RWO claim, one pinned replica. | Reference. |
| `overlays/eks` | Amazon EKS. Swaps in an EFS-backed StorageClass, which the operator must install first. | Adapter example — see [Known limitations](#known-limitations) before using it. |
| `overlays/external-deps` | Control-plane state and object storage outside the cluster (managed database, S3-compatible store). | Example with placeholders. Nothing references it by default; copy it and fill in your own Secrets. |
| `charts/sandbox` | Helm-based installs. `make chart-lint` and `make chart-render` validate it without a cluster. | Independently deployable package. |

Production requires a conforming Kubernetes cluster with a NetworkPolicy-enforcing CNI
and a working gVisor RuntimeClass. Read [Production guide](docs/PRODUCTION.md) and
[Platform capability contract](docs/PLATFORM_CONTRACT.md) before deploying anything
beyond the local profile.

---

## Security boundary

- Agent shell and file operations do not fall back to the host when the Control Plane
  or Runtime is unavailable.
- Runtime Pods are non-root, use a read-only root filesystem, receive no Kubernetes
  service-account token, and run under gVisor in the reference configuration.
- Runtime egress is limited to cluster DNS and public TCP 80/443; private,
  link-local, and loopback ranges are denied.
- Workspace and runtime tokens are scoped: one Workspace cannot read or mutate another.
- Admin credentials are separate from tenant and runtime credentials.
- Object keys are owner-partitioned and object-store credentials are least privilege.
- Checkpoint restore rejects path traversal, links, devices, oversized archives, and
  unexpected archive structure.
- The Control Plane OpenAPI document
  ([`contracts/control-plane-openapi.yaml`](contracts/control-plane-openapi.yaml)) is
  the authority for HTTP routes and authentication groups.

Report vulnerabilities through the process in [SECURITY.md](SECURITY.md). The trust
model, assets, and threat list are in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

---

## Known limitations

These are real and currently unfixed. A README that hides them wastes your time later.

**`overlays/eks` cannot pull images as shipped.** It patches EFS storage onto the base
and nothing else, so the five `imagePullPolicy: Never` settings in `k8s/` survive into
the rendered EKS output. Those exist because the local profile side-loads images into
the VM. On EKS the Pods will never pull from a registry. Patch `imagePullPolicy` and
the image references before treating this overlay as deployable.

**The `sandbox-system` namespace has no NetworkPolicy.** All four shipped
NetworkPolicies target `sandbox-workloads`. The Control Plane and Console are also
exposed as NodePort services (30080 and 30081) in the base manifests. Both are
acceptable for a single-node local VM and are not acceptable on a shared cluster
without an ingress and namespace policies in front of them.

**The workspace admission gate is a process-local lock.**
`_WORKSPACE_ADMISSION_LOCK` in `control_plane/volume.py` serializes workspace
admission within one process. The base `k8s/` manifests and `overlays/eks` run the
volume agent with `replicas: 2` and a RollingUpdate strategy, so two processes hold
two independent locks and the workspace count can be exceeded under concurrency.
`overlays/local` and `overlays/rwo-single-node` patch this to `replicas: 1` with a
`Recreate` strategy and are not affected.

**The Control Plane is a single replica by design.** `k8s/control-plane.yaml` sets
`replicas: 1` with a `Recreate` strategy — deliberate, because several gates are
in-process, but it does mean a control-plane restart is a brief outage.

**No signed release exists yet.** Installation is source-based until the first signed
tag; `main` is not a stable release channel.

[Architecture](docs/ARCHITECTURE.md#scope-boundaries) states the boundaries that were
chosen here rather than merely left undone, and [ROADMAP.md](ROADMAP.md) lists what is
still outstanding before a first public release.

---

## Documentation

| Start here | For |
| --- | --- |
| [Documentation index](docs/README.md) | Everything below, with audience labels |
| [Architecture](docs/ARCHITECTURE.md) · [ADRs](docs/adr/) | Process boundaries, module map, invariants, durable design decisions |
| [Deployment and validation](docs/DEPLOYMENT.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) | Getting it running and fixing it when it is not |
| [HTTP and SDK contract](docs/API.md) · [Authentication contract](docs/AUTH.md) | Writing a client |
| [System specifications](docs/SYSTEM_SPECIFICATIONS.md) · [Configuration](docs/CONFIGURATION.md) | Defaults, limits, environment variables, secrets |
| [Security model](docs/SECURITY_MODEL.md) · [Lifecycle and data](docs/LIFECYCLE_AND_DATA.md) | Trust boundaries, persistence, backup, deletion, recovery |
| [Production guide](docs/PRODUCTION.md) · [Platform capability contract](docs/PLATFORM_CONTRACT.md) · [Compatibility](docs/COMPATIBILITY.md) | Deploying beyond the local profile |
| [Benchmarks](docs/BENCHMARKS.md) · [Benchmark report](docs/BENCHMARK_REPORT_2026-09-01.md) | Reproducible measurement method and results |
| [Release policy](docs/RELEASE.md) · [Supply chain](docs/SUPPLY_CHAIN.md) · [Changelog](CHANGELOG.md) | Versioning, SBOM, signing, provenance, history |

---

## Contributing, support, and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and the checks a
pull request must pass. Changes must preserve workspace ownership, gVisor isolation,
scoped credentials, object-storage boundaries, and fail-closed behavior when the
Control Plane is unavailable.

CI runs on every pull request and every push to `main`: unit and contract tests on
Python 3.11 and 3.14, a wheel build and entry-point smoke test, Console lint,
typecheck, i18n check and build, manifest and Helm rendering, all four container image
builds, a full-history Gitleaks scan, and a Trivy filesystem scan gated at
HIGH/CRITICAL.

- Questions, bugs, and feature requests: [SUPPORT.md](SUPPORT.md)
- Security vulnerabilities: [SECURITY.md](SECURITY.md) — private reporting, not a
  public issue
- Decision ownership: [GOVERNANCE.md](GOVERNANCE.md) and [MAINTAINERS.md](MAINTAINERS.md)
- Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

The repository source is released under the [MIT License](LICENSE), and as of
2026-09-02 no image built from it carries a strong-copyleft component: the Control
Plane used to bundle a patched MinIO Client (`mc`, AGPL-3.0) and now talks S3 through
`boto3` (Apache-2.0). Third-party components and the obligations that still attach to
images built from earlier revisions are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
