# Architecture

## Process boundaries

```text
SDK / CLI / MCP bridge / agent host
        │ HTTPS bearer token
        ▼
Sandbox Control Plane API
  ├── policy and tokens
  ├── state store: PostgreSQL or SQLite
  ├── gVisor Runtime Driver: Pod/Service resources and endpoint
  ├── Workspace Volume Agent: create/list/purge storage
  ├── object client: checkpoints and bounded import/export
  └── reconciler: idle workspace/runtime cleanup
        │
        ▼
gVisor Runtime Pod
  ├── gVisor sandbox
  ├── non-root process
  ├── read-only root filesystem
  └── shell MCP: exec, stream, PTY
```

## Scope boundaries

What this project owns, and what it deliberately leaves to a caller or an operator.
These are the boundaries that were chosen, not merely the work that is undone.

- Sandbox owns execution, Workspace lifecycle, Runtime isolation, its own database
  schema, and protocol-neutral S3 usage.
- Kubernetes and cloud node lifecycle, CNI, CSI, Ceph deployment and the collection
  stack are somebody else's. Sandbox only declares resources, placement constraints,
  and telemetry artifacts; see [Platform capability contract](PLATFORM_CONTRACT.md).
- Every consumer authenticates as an external tenant or as an operator. No neighbouring
  project receives the Sandbox break-glass credential; see
  [Authentication contract](AUTH.md).
- Workspace is durable state, Runtime is disposable compute. A worker scale-down may
  therefore evict Runtime Pods: file persistence is not node-local. The per-resource
  consequences are tabulated in [Lifecycle and data](LIFECYCLE_AND_DATA.md).

## Module map

| Path | Boundary |
| --- | --- |
| `control_plane/server.py` | Process entrypoint; selects API or volume-agent role through `SANDBOX_CONTROL_PLANE_ROLE` |
| `control_plane/core.py` | Domain orchestration for configuration, policy, tokens, and lifecycle state |
| `control_plane/api.py` | HTTP routing and authorization composition |
| `control_plane/runtime_driver.py` | Provider-neutral Runtime specification, capabilities, and infrastructure contract |
| `control_plane/drivers/gvisor.py` | The only Runtime Driver in this release: Kubernetes Pod/Service operations through gVisor RuntimeClass |
| `control_plane/oidc.py` | OpenID Connect relying party: Authorization Code + PKCE against the deployment's own provider |
| `control_plane/session.py` | Signed Console browser session, minted only by the OIDC callback |
| `control_plane/tracing.py` | W3C `traceparent` propagation: inbound adoption, outbound continuation, `trace_id` for logs |
| `capability_ticket.py` | Shared issuer/verifier for internal Control Plane -> sandbox tickets; copied into all three images. Why a per-instance key rather than the signing key or a control-plane lookup: [ADR 0005](adr/0005-sandbox-capability-tickets.md) |
| `control_plane/store.py` | PostgreSQL/SQLite persistence |
| `control_plane/kube.py` | Minimal Kubernetes API client |
| `control_plane/manifests.py` | Runtime resources |
| `control_plane/metrics.py` | Dependency-free Prometheus text-format counters and gauges |
| `control_plane/reaper.py` | Expiration actions |
| `control_plane/volume.py` | Shared-volume operations |
| `runtime/runtime_server.py` | Shell MCP entry |
| `runtime/shell_sessions.py` | PTY session protocol and state |
| `file-service/file_service.py` | Canonical Workspace file operations imported in-process by the Runtime; mutable file requests have no offline fallback |
| `workspace_contract.py` | Shared file-operation limits copied into the Control Plane, Runtime, and file-service images; a contract test keeps the copies identical |
| `contracts/control-plane-openapi.yaml` | Control Plane HTTP and authentication contract |
| `sandbox_platform/` | Installable Python package: SDK (`sandbox_client`), user CLI (`sandbox_cli`), operator CLI (`sandboxctl`), stdio MCP adapter (`mcp`), shared HTTP transport (`control_plane_transport`), and the bounded-queue stdout guard used by long-lived processes (`safe_stdout`) |
| `console/` | Static operator console (Vite/React) served by nginx; talks only to the Control Plane HTTP API |

## Terminology

| Term | Meaning |
| --- | --- |
| Workspace | Persistent, owner-scoped file storage identified by a `ws-` id derived from the session identity and `WORKSPACE_ID_KEY`. Survives Runtime release. |
| Runtime | One gVisor Pod bound to a Workspace, identified by an `sb-` id. On the HTTP surface it is called a *sandbox* (`/v1/sandboxes/{id}`). Has idle and absolute TTLs. |
| `Sandbox` (SDK) | The `sandbox_client.Sandbox` facade: a *named Workspace* whose current Runtime is created on demand. `Sandbox.stop()` releases the Runtime and keeps the files. |
| Session | Three distinct things share this word. **Control Plane `session_id`**: the caller-supplied identity from which a Workspace id is derived. **SDK `SANDBOX_SESSION_ID`**: the environment variable that supplies that identity to the SDK and MCP bridge (a `Sandbox` name plays the same role). **Runtime `shell_session`**: a persistent PTY inside one Runtime, addressed by the `session_id` argument of the `shell_session` tool. |
| Checkpoint | A validated archive of Workspace files stored in the object store. Not a VM, process, or memory snapshot. |
| Template | An approved Runtime image reference, from `SANDBOX_TEMPLATES` or the admin API. |
| Object ticket | A single-use, object-bound credential for one upload or download. |

### Legacy comment vocabulary

Older comments were machine-translated and still use a few words that do not match
the terms above. They are being translated back; until then read them as follows:

| Comment says | Read as |
| --- | --- |
| library | database / store (`control plane.STORE`), never a Python import |
| box, "box building" | Runtime Pod, Runtime creation |
| character, role | process role selected by `SANDBOX_CONTROL_PLANE_ROLE` |
| account | tenant |
| card, record | row in the store |

## Request and resource lifecycle

```text
SDK / CLI / stdio MCP
        │ Control Plane or tenant credential
        ▼
Control Plane authenticates and resolves owner
        │
        ├── ensure Workspace ownership and storage
        ├── admit or reuse a Runtime Pod
        ├── wait for Runtime health
        └── issue a short-lived workspace/runtime token
                         │
                         ▼
              Runtime MCP inside gVisor
                 shell / PTY / files
```

Runtime and Workspace lifetimes are deliberately separate. Releasing or reaping a
Runtime removes compute and process state; Workspace files remain until their own
retention or explicit purge. Checkpoints are file archives in object storage, not VM
or memory snapshots. See [Lifecycle and data](LIFECYCLE_AND_DATA.md).

## Deployment profiles

| Profile | Isolation and dependencies | Intended use |
| --- | --- | --- |
| Local integration | gVisor, Cilium, SQLite, local-path Workspace PVC, and cluster-local Ceph RGW | Reproducible isolation and E2E validation |
| Operator production | gVisor nodes, enforcing CNI, PostgreSQL, external RWX and S3-compatible storage | ACK, EKS, or any conforming Kubernetes platform |

The same Control Plane/Runtime contract applies across profiles. The local profile proves
the gVisor boundary but not production storage durability or high availability.

The Sandbox object plane is independently deployable. In a larger agent platform it
does not need to share physical S3 storage with the durable file library. A federated
integration streams objects through Control Plane's short-lived, single-use upload or
download tickets; no peer receives the other's S3 credentials. A shared storage plane
is valid only when both services intentionally address the same objects.

## Invariants

- The Control Plane is the only component permitted to create, release, and rebind workspaces.
- Runtime provider details enter orchestration only through `RuntimeDriver`;
  policy and HTTP layers consume `RuntimeInstance`/`RuntimeUsage` values and
  provider-neutral `RuntimeDriverError` categories, never Pod dictionaries or
  Kubernetes client errors.
- This release registers only the gVisor Driver and exposes no suspend/resume capability.
- Runtime receives short-lived scoped tokens, not Control Plane admin credentials.
- Runtime has no Kubernetes API client and no service-account token.
- File operations reject absolute paths, traversal, resolved escapes, and workspace-reserved paths.
- Checkpoint restore validates archive structure, paths, links, devices, and size before replacing a workspace.
- Control Plane failure is surfaced as an error; it never enables a host execution fallback.

## Component naming

The component is named `Sandbox Control Plane` throughout the source package,
image, Kubernetes resources, token audience, client configuration, and
observability artifacts. Process roles are `api` and `volume`; the Runtime
provider is selected independently by `SANDBOX_RUNTIME_DRIVER`.

## Known split debt

The repository is self-contained: build, test, and local-cluster entrypoints
resolve paths from this checkout.

Several control-plane modules remain larger than their domain boundaries. Future
refactors should extract authentication, Workspace, Runtime, object, and admin route
groups without changing the OpenAPI contract or authorization behavior. Route,
security policy, and handler registration should ultimately have one declarative
source of truth. Until that migration has component-level tests, keep changes narrow
and preserve the `ROUTE_AUTH` contract gate.

Durable design constraints are recorded in [architecture decision records](adr/README.md)
so current-code comments can stay concise.
