# Sandbox Platform

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Run an agent's shell and file operations inside a gVisor Pod on your own Kubernetes
cluster.** A Control Plane owns Workspaces, quotas, and credentials. Agents reach it
through one of three published surfaces — a Python SDK, the `sandbox` CLI, or a stdio
MCP bridge — and nothing else. When the Control Plane or the Runtime is unreachable,
the operation fails; it never falls back to executing on the host.

> **Status: alpha (`0.1.0`).** No signed release has been published yet, so `main` is
> the only channel and it is not a stability promise. See [ROADMAP.md](ROADMAP.md) for
> the remaining pre-release work and [Known limitations](#known-limitations) for what is
> honestly not finished.

> **Where this fits.** Sandbox Platform is one of four independently released
> repositories. [`convee/platform-composition`](https://github.com/convee/platform-composition)
> is the only place that describes all four together: what each one is, where the
> boundaries between them are, and how to install the set on an enterprise cluster.
> This README does not repeat any of that — it is about Sandbox Platform alone.

---

## Contents

- [Why this instead of the alternatives](#why-this-instead-of-the-alternatives)
- [Evaluate it in one minute, no cluster required](#evaluate-it-in-one-minute-no-cluster-required)
- [Run the full local cluster](#run-the-full-local-cluster)
- [Using it: SDK, CLI, MCP, Console](#using-it)
- [Common tasks](#common-tasks)
- [Architecture](#architecture)
- [Deployment profiles](#deployment-profiles)
- [Security boundary](#security-boundary)
- [Known limitations](#known-limitations)
- [Documentation](#documentation)
- [Contributing, support, and license](#contributing-support-and-license)

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

## Evaluate it in one minute, no cluster required

The full local profile needs a virtual machine with real resources (see the next
section). If you only want to judge whether the project is worth your time, none of
that is necessary. Each command below runs on an ordinary Linux or macOS checkout with
Python 3.11+ installed.

```bash
python3 -m venv .venv                  # a system-wide install is an error on Debian,
.venv/bin/pip install -e '.[test]'     # Ubuntu and Fedora (PEP 668)
make test                      # 658 unit and contract tests, no network, no cluster
make help                      # every Make target with its one-line description
```

`make test` uses `.venv/bin/python` when that virtual environment exists and falls
back to `python3` otherwise, so no `PATH` or activation step is needed.

Inspect the surfaces without a running Control Plane:

```bash
sandbox --help          # create / run / exec / stop / list
sandboxctl --help       # operator surface: workspaces, templates, admin keys, audit
sandbox-mcp --help      # required env vars and the nine agent-scoped MCP tools
```

Render the Kubernetes and Helm artifacts (needs `kubectl` and `helm`; no cluster
contact):

```bash
kubectl kustomize k8s
kubectl kustomize overlays/local
make chart-lint
make chart-render
```

Preview the operator Console against its in-memory fake backend (needs Node.js and
npm; no cluster):

```bash
npm --prefix console ci --ignore-scripts
VITE_USE_MOCK=1 npm --prefix console run dev -- --host 127.0.0.1
```

In mock mode, sign in as `admin`, `tenant`, `breakglass`, or `nowhitelist` to switch
identities; any other value returns 401.

---

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

### Python SDK

```bash
export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
python3 - <<'PY'
from sandbox_platform.sandbox_client import Sandbox

sandbox = Sandbox.get_or_create("demo")
result = sandbox.run_command("printf", ["sandbox-ready\\n"])
print(result.stdout, end="")
sandbox.stop()  # Runtime stops; Workspace files remain.
PY
```

`Sandbox` is the high-level facade. The lower-level `SandboxManager` — exported as
`sandbox_client.MANAGER` — carries the object-store, checkpoint, and shell-session
operations used in [Common tasks](#common-tasks).

### CLI

```bash
sandbox create demo
sandbox exec demo -- python -c 'print("sandbox-ready")'
sandbox run --name demo --stop -- sh -lc 'printf "done\\n"'
sandbox list
```

`sandbox create` prints the name, Runtime id, and Workspace id separated by tabs
(`demo<TAB>sb-...<TAB>ws-...`). `sandbox list` prints one active Runtime per line as
`id`, `workspace_id`, `status`, `template`. Both accept `--json` for machine output.

`sandbox` is the user surface. `sandboxctl` is a separate operator surface and may
expose tenant-wide administrative data; keep the two credentials distinct.

### MCP bridge

`sandbox-mcp` is a stdio MCP server. It requires three environment variables and
refuses to start without them:

| Variable | Meaning |
| --- | --- |
| `SANDBOX_CONTROL_PLANE_URL` | Control Plane base URL, for example `http://127.0.0.1:18080` |
| `SANDBOX_TOKEN` | Control Plane or tenant bearer token |
| `SANDBOX_SESSION_ID` | Any stable string identifying this agent session; it selects the Workspace the session lands in |

Claude Code:

```bash
claude mcp add sandbox \
  -e SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080 \
  -e SANDBOX_TOKEN="$(make --no-print-directory dev-token)" \
  -e SANDBOX_SESSION_ID=my-project \
  -- sandbox-mcp
```

Any host that reads an `mcpServers` object:

```json
{
  "mcpServers": {
    "sandbox": {
      "command": "sandbox-mcp",
      "env": {
        "SANDBOX_CONTROL_PLANE_URL": "http://127.0.0.1:18080",
        "SANDBOX_TOKEN": "<paste the value of make dev-token>",
        "SANDBOX_SESSION_ID": "my-project"
      }
    }
  }
}
```

From a source checkout without installing the wheel, use
`python3 -m sandbox_platform.mcp` as the command with the same environment.

The bridge exposes exactly nine agent-scoped tools: `shell`, `shell_session`,
`sandbox_status`, `file_read`, `file_write`, `file_edit`, `file_glob`, `file_grep`,
and `workspace_checkpoint`. Tenant, key, template, audit, and arbitrary Runtime
administration are deliberately outside the agent surface.

### Operator Console

Keep this running and open <http://127.0.0.1:18081>:

```bash
make console-forward
```

The Console embeds no credential; a key you paste stays in tab-scoped
`sessionStorage`. For the local profile, paste the value of `make dev-token` — it is
cluster-administrator-equivalent and is intended only for this profile.

A deployment offers whichever of these sign-in methods it has configured:

- **Single sign-on.** Set `SANDBOX_CONTROL_PLANE_OIDC_*` and the Control Plane runs an
  OpenID Connect Authorization Code + PKCE flow against your provider. It is a relying
  party only: it issues assertions for nobody and accepts none.
  `SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE` has no default — give this Control Plane an
  audience no other service shares, or an ID token minted for a neighbouring service
  is accepted here too.
- **An API key.** Keys issued through `/v1/admin/keys` are revocable, attributable,
  and can carry an expiry and a permission set. This is the route for machine callers
  and the everyday route for people without SSO.
- **`SANDBOX_CONTROL_PLANE_TOKEN`, the break-glass token.** The way in when the
  identity provider is unreachable. It is administrator-equivalent, cannot be revoked,
  and cannot be attributed to a person — so every use is written to the Control Plane
  log with its source address, counted under
  `sandbox_credential_uses_total{kind="break-glass"}`, and shown as a banner for the
  whole Console session. It is off by default whenever an OIDC provider is configured,
  and switching it off removes the credential from the process: the API refuses it, not
  just the login form. Configuring no provider *and* switching it off makes the
  Control Plane refuse to start.

That escape hatch is documented on purpose. One that exists only in the source is one
the operators do not know about and an attacker reading the code does.

Configuration details are in the [configuration reference](docs/CONFIGURATION.md); the
[authentication contract](docs/AUTH.md) is the published agreement for client authors.

The Console ships English and Simplified Chinese. It follows the browser language on
first use and stores an explicit choice in `localStorage` under
`sandbox-console-language`. Catalogs live in `console/src/i18n/locales`, typed against
the English source so a missing key fails `npm --prefix console run typecheck`; adding
a locale means adding its code to `LOCALES` in `console/src/i18n/index.tsx`, adding a
catalog, and adding the option to `LanguageSwitcher`.

---

## Common tasks

Every method named here exists in
[`sandbox_platform/sandbox_client.py`](sandbox_platform/sandbox_client.py) and
[`sandbox_platform/sandbox_cli.py`](sandbox_platform/sandbox_cli.py). Numeric limits
come from [System specifications](docs/SYSTEM_SPECIFICATIONS.md).

**Upload a file.** Text up to 1 MB goes straight into the Workspace:

```python
sandbox.write_file("input/data.csv", csv_text)
sandbox.write_files([{"path": "a.txt", "content": "1"}, {"path": "b.txt", "content": "2"}])
```

Larger or binary inputs go through the object store: `MANAGER.put_agent_blob(...)`
uploads with a single-use ticket, then
`MANAGER.import_object_to_workspace(locator, "input/data.bin")` copies the object into
the Workspace.

When an agent host keeps its durable file library in a different S3 plane, the
recommended integration is federated transfer: the host's artifact service streams to
and from these same ticketed Control Plane endpoints. The host never receives the
Sandbox S3 credential and the Sandbox never receives the host's. Deliberately sharing
one physical bucket is also possible, but that is a decision the integrating platform
owns.

**Run a long task.** One `shell` call is capped at 30 seconds
(`MAX_EXEC_TIMEOUT_SECONDS` in `runtime/runtime_server.py`). For anything longer,
start a PTY session and poll it; a session may live up to one hour of wall time:

```python
MANAGER.shell_session("exec", "build", command="make -j4 test", async_mode=True)
result = MANAGER.shell_session("wait", "build", timeout_seconds=120)  # repeat until done
```

From the CLI, `sandbox exec demo --timeout 30 -- <command>` uses the same 30-second cap.

**Get artifacts back.** `sandbox.read_file("out/report.txt")` returns bounded text. For
whole files, `MANAGER.export_workspace_object("out/report.pdf", locator)` writes the
file to the object store and `MANAGER.open_object(locator)` downloads it. For a
directory of many small deliverables,
`MANAGER.export_workspace_collection("artifacts", locator)` creates one bounded
`tar.gz` with a `manifest.json` of member paths, sizes, and SHA-256 values. It exports
regular files only and omits internal state, legacy compaction data, and symlinks.

**Checkpoint and restore.** Checkpoints are explicit recovery points, not the normal
file-persistence path — the Workspace PVC already survives Runtime release.
`sandbox.checkpoint()` archives the Workspace *files* (not processes or memory) into
object storage; `MANAGER.list_workspace_checkpoints()` lists archives and
`MANAGER.restore_workspace(checkpoint_id)` replaces the Workspace with one. The MCP
tool `workspace_checkpoint` exposes the same archive operation to agents.

**Run several sandboxes in parallel.** Each name is an independent Workspace with its
own Runtime; `Sandbox.create("job-1")` and `Sandbox.create("job-2")` share no files.
The reference deployment admits 4 concurrent Runtimes (`SANDBOX_MAX_RUNTIMES`) and 64
Workspaces (`SANDBOX_MAX_WORKSPACES`); `sandbox list` shows what is running.

**Install packages.** The default Runtime image deliberately ships without `pip` or any
package manager ([`runtime/Dockerfile`](runtime/Dockerfile) removes pip after the build
so its vendored dependencies and installation attack surface go with it). The image
carries Python 3.14 with the pptx/docx/xlsx/pdf libraries, Node.js 24 with npm, plus
bash, git, curl, jq, make, and unzip. To add packages, build your own image, register
it under `SANDBOX_TEMPLATES`, and pass `--template <id>` to `sandbox create` or
`template=` to `Sandbox.create`.

---

## Architecture

```text
Agent host
    │ Python SDK, sandbox CLI, or stdio MCP
    ▼
Sandbox Control Plane ─── PostgreSQL, MySQL, or SQLite state
    │ policy, tokens, workspace/runtime lifecycle
    ├──► gVisor Runtime Driver ── Kubernetes RuntimeClass
    ├──► Workspace Volume Agent ─── Workspace PVC
    ├──► Runtime Pod (gVisor) ── shell, files, PTY
    └──► cluster-local Ceph RGW ── explicit checkpoints and bounded imports/exports
```

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
through the `mc` command-line client, which has no header injection point, so that hop
is untraced. Someone who finds a hole in a trace should be able to confirm it is
expected instead of first suspecting their own query.

---

## Deployment profiles

Kustomize overlays under [`overlays/`](overlays/) layer on the provider-neutral base in
[`k8s/`](k8s/). All of them render with `kubectl kustomize <path>`; CI checks that on
every push.

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

**AGPL-licensed `mc` is a runtime dependency of the Control Plane image.** Every
object-store operation — checkpoints, imports, exports, tickets, garbage collection —
runs through a `/usr/local/bin/mc` subprocess. There is no S3 SDK in the code path.
The image ships the AGPL-3.0 text, a `SOURCE` file naming the upstream commit and the
tarball SHA-256, and a copy of the applied patch, which together satisfy the source
obligation — but **anyone redistributing that image conveys a modified AGPL-3.0 work**
and inherits those obligations. Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
before publishing a derivative image. Note that replacing MinIO Server with Ceph RGW
changed the *server* side only; the client is still `mc`.

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

The repository source is released under the [MIT License](LICENSE). The Control Plane
**image** additionally bundles a patched build of MinIO Client (`mc`, AGPL-3.0) as a
runtime dependency; distributors of that image must satisfy the obligations described
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). `mc` is used purely as an S3
protocol client — Sandbox Platform does not deploy MinIO Server.
