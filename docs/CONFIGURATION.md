# Configuration reference

Configuration is supplied through Kubernetes ConfigMaps and Secrets. Never put
credentials in Git, command history, the operator console, or workspace volumes.

## Scope

This document lists every environment variable the shipped code reads, for all
four process roles: the **Control Plane** (`SANDBOX_CONTROL_PLANE_ROLE=api`),
the **volume** role of the same image (`SANDBOX_CONTROL_PLANE_ROLE=volume`),
the **Runtime** server inside each sandbox Pod, and the **file-service** that
runs beside it (and, as a CronJob, garbage-collects workspaces). Client-side
variables read by the SDK, `sandboxctl`, the MCP server and the benchmark
runner are listed at the end. Defaults are the values in the source; a test
(`tests/test_configuration_reference.py`) fails when the code reads a name that
appears in neither this document nor [SYSTEM_SPECIFICATIONS.md](SYSTEM_SPECIFICATIONS.md),
where the capacity limits live.

## Where a setting belongs

| Class | Examples | Owner | Runtime editable |
| --- | --- | --- | --- |
| Code invariant | path traversal rules, token audience/signature checks, Runtime security context, Grafana proxy allowlist | Product source and security review | No |
| Deployment/GitOps | RuntimeClass, node selector, image references, database/S3 endpoint, OIDC issuer, OTLP endpoint, global resource ceilings | Cluster operator | No; roll out declaratively |
| Management plane policy | tenant runtime/workspace quota, tenant suspension, API key permissions/expiry, approved templates | Sandbox administrator | Yes, audited |
| Tenant request | Workspace name, selected approved template, per-run TTL within the administrator ceiling | Authenticated tenant | Yes, bounded by policy |

The management UI must not become a second deployment system. In particular it
must never accept credentials, arbitrary image registries, RuntimeClass names,
node selectors, signing algorithms or security-context fields. Those values alter
the trust boundary and remain reviewable GitOps configuration. Safe tenant policy
belongs in the authenticated admin API because changing it should not require a
Pod rollout and every change already has an audit identity.

## Required secrets

| Secret value | Purpose | Rotation note |
| --- | --- | --- |
| `SANDBOX_CONTROL_PLANE_TOKEN` | Break-glass administrator credential | Required only while local login is on; unread otherwise. Never a signing key. Every use is logged (see [Sign-in methods](#sign-in-methods)) |
| `SIGNING_KEY` | Signs scoped Runtime tokens and object tickets, derives sandbox capability keys and the Console session subkey | Rotatable, but without overlap: tokens are verified against one key, so every in-flight access token (TTL 900s) becomes invalid the moment the key changes; rotate at low traffic |
| `WORKSPACE_ID_KEY` | Stable workspace-ID derivation | Required, no fallback to `SIGNING_KEY`, cannot be rotated: a new value changes every derived workspace ID and orphans existing workspace directories. Deployments that ran before this key existed must set it to their `SIGNING_KEY` value (the bootstrap script does this) |
| `VOLUME_AGENT_TOKEN` | Control Plane-to-volume-agent authentication | Rotate both namespaces together |
| Object-store access/secret keys | Checkpoint and object access | Scope to the configured bucket/prefix only |
| Database password | PostgreSQL or MySQL control-plane state | Mount from a Secret file |

The development bootstrap script creates random values without printing them. A
production secret manager and audited rotation procedure are operator requirements.

## Sign-in methods

Browser identity comes from the deployment's own OpenID Connect provider; Control Plane
is a relying party and never an issuer. Machine callers use API keys issued by
the control plane, which are unaffected by anything in this section.

This section is the operator's side. The client-facing side of the same contract
- what an integrator may rely on and how they obtain credentials - is the
[authentication contract](AUTH.md).

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED` | on without a provider, off with one | Whether the static `SANDBOX_CONTROL_PLANE_TOKEN` is accepted at all. **Nothing else.** See the warning below |
| `SANDBOX_CONTROL_PLANE_OIDC_ISSUER` | empty | Provider issuer URL; setting it turns the Console single-sign-on button on |
| `SANDBOX_CONTROL_PLANE_OIDC_CLIENT_ID` | empty | Client registered for this Control Plane |
| `SANDBOX_CONTROL_PLANE_OIDC_CLIENT_SECRET` | empty | Optional; a public client uses PKCE alone |
| `SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE` | **none** | Audience this Control Plane accepts in an ID token. No default and no fallback to the client id: give it a value no neighbouring service shares, or a token minted for that service is spendable here |
| `SANDBOX_CONTROL_PLANE_OIDC_REDIRECT_URL` | empty | Must be `https://<console host>/v1/auth/oidc/callback` |
| `SANDBOX_CONTROL_PLANE_OIDC_SCOPES` | `openid email profile` | Must include `openid` |
| `SANDBOX_CONTROL_PLANE_OIDC_GROUPS_CLAIM` | `groups` | Claim carrying group membership |
| `SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS` | empty | Space-separated groups mapped to the management plane |
| `SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM` | empty | Claim naming the tenant a user signs in to; the tenant must already exist |
| `SANDBOX_CONTROL_PLANE_OIDC_ALLOW_INSECURE_HTTP` | `false` | Permits `http://` loopback endpoints for local development only |

Three rules decide what happens at startup:

* a provider configured, `SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED` unset -> the static token
  is **off**; it can be switched back on explicitly for emergencies;
* no provider -> the static token is **on**, because nothing else could sign in;
* no provider and the static token switched off -> Control Plane **refuses to start**
  and says so, rather than running with no way in.
* whenever local login is off, `SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS` must
  contain at least one management-plane mapping. A tenant-claim-only provider is
  rejected because tenant identities cannot create the first tenant, admin key,
  or another administrator. Tenant-only OIDC remains valid when the break-glass
  administrator path is explicitly kept on.

Switching it off removes the credential from the process, so the API refuses it
too. Hiding the field in the Console is not the control; do not treat it as one.

Constraint: **`SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED=false` does not close every non-OIDC way in.**
It governs the static `SANDBOX_CONTROL_PLANE_TOKEN` and nothing else. API keys issued by this
control plane keep authenticating exactly as before, and they are meant to:
they are how any external service calls this platform, and they are revocable,
attributable and expiring, which is precisely what the static token is not.
"How a person signs in to this deployment" and "how another organization's
service calls it" are two questions, and one switch answering both would mean
turning off a login form also cut off every integrator.

Read the switch as *"is the emergency door bricked up"*, not *"is everything
except single sign-on turned off"*. To reduce what API keys can do, act on the
keys - revoke them (`DELETE /v1/admin/keys/{key_id}`), issue them with
`expires_in_seconds`, and do not grant permissions they do not need. `GET
/v1/admin/keys` lists every key that currently exists, which is the only
authoritative answer to "what can still get in".

The failure this warning exists to prevent is believing a door is shut when it
is open. Confirm it instead of assuming it: `GET /v1/auth/methods` reports
`local_login`, and a request carrying the static token answers `401` once it is
off.

`SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS` or `SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM` must be set: an identity
that maps to neither is refused. Nothing is created on the strength of a login -
a tenant claim naming a tenant that does not exist, or one that is suspended,
ends in `403`.

### The break-glass token

`SANDBOX_CONTROL_PLANE_TOKEN` is the way in when the identity provider is unreachable. It is
administrator-equivalent, belongs to no tenant, cannot be revoked and cannot be
attributed to a person, so:

* every request that uses it writes `auth break-glass SANDBOX_CONTROL_PLANE_TOKEN source=...`
  to the Control Plane log, including the source address and the route;
* `sandbox_credential_uses_total{kind="break-glass"}` counts them, which is how
  "is anybody still on this path" gets answered;
* the Console shows a banner for the whole session;
* it signs nothing. Console sessions are signed with a subkey derived from
  `SIGNING_KEY`, and sandbox capability keys likewise - one key, one purpose.

## Core Control Plane settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_CONTROL_PLANE_ROLE` | `api` | Process role: `api` or `volume` |
| `SANDBOX_STORE_BACKEND` | empty | Empty is legacy single-tenant; use `sqlite` for local development, or `postgresql` / `mysql` for durable/multi-tenant state (see below) |
| `SANDBOX_DB_CONNECT_TIMEOUT` | `5` | Seconds to wait for a PostgreSQL/MySQL connection |
| `SANDBOX_DB_STATEMENT_TIMEOUT_MS` | `5000` | PostgreSQL `statement_timeout`; on MySQL the socket read/write deadline, rounded up to whole seconds |
| `SANDBOX_DB_IDLE_TX_TIMEOUT_MS` | `10000` | PostgreSQL `idle_in_transaction_session_timeout`; not applied on MySQL |
| `SANDBOX_STORE_LOCK_TIMEOUT_SECONDS` | `10` | Seconds a request waits for the process-wide store lock before answering `503` |
| `SANDBOX_WORKSPACE_STORAGE_MODE` | `shared` | `shared`, or an operator-provided `per-workspace` topology |
| `SANDBOX_RUNTIME_DRIVER` | `gvisor` | Runtime Driver selected by the Control Plane. Only `gvisor` is accepted in this release |
| `SANDBOX_RUNTIME_CLASS` | `gvisor` | RuntimeClass name; an empty value disables the field and removes the isolation guarantee |
| `SANDBOX_RUNTIME_NODE_SELECTOR` | empty | Comma-separated `key=value` pairs for Runtime placement |
| `SANDBOX_RUNTIME_TOLERATIONS` | empty | JSON array of explicit Kubernetes tolerations for Runtime Pods; do not derive trust-boundary tolerations implicitly from labels |
| `SANDBOX_TEMPLATES` | built-in default only | JSON object mapping approved template IDs to image references |
| `SANDBOX_IMAGE_REGISTRIES` | empty | Allowed literal image prefixes; empty fails closed for API-managed templates |
| `SANDBOX_MAX_WORKSPACES` | `64` | Global workspace admission cap |
| `SANDBOX_MAX_RUNTIMES` | `4` | Global Runtime admission cap |
| `SANDBOX_TTL_SECONDS` | `1800` | Idle Runtime TTL |
| `SANDBOX_RUNTIME_HARD_TTL_SECONDS` | `43200` | Absolute Runtime lifetime |
| `WORKSPACE_IDLE_TTL_SECONDS` | `21600` | Workspace data idle TTL |
| `SANDBOX_WORKSPACE_QUOTA` | `1Gi` | Requested PVC size in optional per-workspace mode |
| `SANDBOX_PENDING_STALE_SECONDS` | `600` | Age after which a `pending` Runtime admission record is treated as abandoned and its slot released; keep well above the Runtime creation budget |
| `SANDBOX_ACTIVITY_PROBE_TIMEOUT` | `2` | Seconds allowed for the Runtime activity check that runs before an idle Runtime is evicted |
| `SANDBOX_MAX_OBJECT_QUEUE` | `32` | Requests allowed to wait for the object-store slot; beyond it Control Plane answers `503` at once |
| `SANDBOX_MAX_CONCURRENT_OBJECT_OPS` | `1` | Object-store operations in flight at once; each holds its body in memory, and the boto3 connection pool is sized to match |
| `SANDBOX_MAX_LIST_ENTRIES` | `10000` | Rows one listing may return before it is refused. `read_timeout` bounds a single socket read, not an operation, so without this a slow trickle holds the operation slot indefinitely while the list grows in memory |
| `SANDBOX_STORE_GAUGE_TTL_SECONDS` | `10` | Cache lifetime of the store-backed `/metrics` gauges, so scrapes do not each query the database |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_DRAIN_SECONDS` | `5` | After SIGTERM, seconds `/readyz` reports `503` before listening stops, so Endpoints drop the Pod first |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_INFLIGHT_SECONDS` | `120` | Seconds to wait for in-flight requests and background Runtime creation after listening stops |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_REAPER_SECONDS` | `60` | Seconds to wait for the reaper to finish its current round; the sum of the three shutdown values must stay below `terminationGracePeriodSeconds` |
| `SANDBOX_IDLE_EVICT_SECONDS` | `300` | When the Runtime pool is full, a Runtime idle longer than this is released early to make room instead of answering `429`; shorter than the TTL |
| `SANDBOX_MAX_INFLIGHT_CREATES` | `SANDBOX_MAX_RUNTIMES` | Background Runtime creations allowed at once; each holds a thread, so this bounds memory under a burst |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | Lifetime of the scoped Runtime access tokens the Control Plane mints |
| `OBJECT_TICKET_TTL_SECONDS` | `900` | Upper bound on an object ticket's `expires_in`; every ticket is single-use through a Kubernetes Lease |
| `MAX_STREAM_OBJECT_BYTES` | `67108864` (64 MiB) | Largest object streamed through the Control Plane |
| `CHECKPOINT_RETENTION_SECONDS` | `2592000` (30 days) | Age past which the checkpoint GC deletes a checkpoint |
| `CHECKPOINT_GC_INTERVAL_SECONDS` | `3600` | Seconds between checkpoint GC rounds |

### Listening and topology

The Control Plane needs to know where it runs. These names are set by the
manifests in `k8s/`; change them together with the resources they name.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_CONTROL_PLANE_HOST` | `0.0.0.0` | Listen address |
| `SANDBOX_CONTROL_PLANE_PORT` | `8080` | Listen port |
| `SANDBOX_NAMESPACE` | `sandbox-workloads` | Namespace Runtime Pods, Services and workspace PVCs are created in |
| `SANDBOX_SYSTEM_NAMESPACE` | `sandbox-system` | Namespace the Control Plane itself runs in |
| `SANDBOX_RUNTIME_IMAGE` | see [SYSTEM_SPECIFICATIONS.md](SYSTEM_SPECIFICATIONS.md) | Image for the built-in default template |
| `WORKSPACE_PVC` | `sandbox-workspaces` | Name of the shared RWX claim mounted with a per-workspace `subPath` in `shared` storage mode |
| `SANDBOX_RWX_STORAGE_CLASS` | `sandbox-rwx` | StorageClass for the PVC created per workspace in `per-workspace` storage mode |
| `SANDBOX_CONTROL_PLANE_WORKSPACE_ROOT` | `/workspaces` | Mount point of the whole workspace volume; only the volume role mounts it |
| `VOLUME_AGENT_URL` | empty | Service URL of the volume role. Empty means no volume agent: a file read while the Runtime is absent answers `409` instead of pretending the file does not exist |
| `VOLUME_AGENT_TOKEN` | empty for `api`, required for `volume` | Shared secret between the two roles (Secret `sandbox-volume-auth`). Deliberately not `SIGNING_KEY`, which must never enter `sandbox-workloads` |
| `SANDBOX_STORE_PATH` | `/tmp/sandbox-control-plane.db` | SQLite file when `SANDBOX_STORE_BACKEND=sqlite`; local development only |

### Kubernetes API access

The `api` role talks to the Kubernetes API with its Pod service account. The
first name is injected by Kubernetes into every Pod and is required; the rest
default to the standard projected-token paths.

| Variable | Default | Meaning |
| --- | --- | --- |
| `KUBERNETES_SERVICE_HOST` | injected by Kubernetes | API server host; startup fails without it |
| `KUBERNETES_SERVICE_PORT_HTTPS` | `443` | API server port |
| `KUBERNETES_TOKEN_FILE` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | Service-account token file |
| `KUBERNETES_CA_FILE` | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | CA bundle used to verify the API server |

### Object store

Any S3-compatible endpoint works. Buckets are not created by the Control
Plane; the storage side initialises them and only the names are configured
here. The first three have no default: a Control Plane started without them
exits after listing every missing name (ConfigMap `object-store-config`,
Secret `object-store-credentials`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBJECT_STORE_ENDPOINT` | **required** | S3 endpoint URL: a Service DNS name, `http://<NodeIP>:<NodePort>`, or a hosted endpoint |
| `OBJECT_STORE_ACCESS_KEY` | **required** | Access key, from the Secret |
| `OBJECT_STORE_SECRET_KEY` | **required** | Secret key, from the Secret |
| `OBJECT_STORE_UPLOAD_BUCKET` | `user-uploads` | Bucket for user uploads |
| `OBJECT_STORE_AGENT_BUCKET` | `agent-data` | Bucket for agent-produced objects |
| `OBJECT_STORE_WORKSPACE_BUCKET` | `sandbox-workspaces` | Bucket for workspace checkpoints |
| `OBJECT_STORE_HEALTH_PATH` | empty | Anonymously reachable health path on the endpoint, probed by `/healthz`. Empty skips the probe: a vendor path such as `/minio/health/ready` is `404` on every other implementation and would fail the probe forever |
| `OBJECT_STORE_SIGNATURE_VERSION` | `S3v4` | Request signing version, `S3v2` or `S3v4` |
| `OBJECT_STORE_ADDRESSING_STYLE` | `auto` | Bucket addressing, `auto`, `virtual`, or `path` |

### Trace export

Span export follows the OpenTelemetry environment names. Only OTLP/HTTP JSON
is implemented; naming another protocol while an endpoint is set fails
startup. The `*_TRACES_*` name wins over the generic one where both exist.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | empty | Full OTLP/HTTP JSON `/v1/traces` endpoint; empty falls back to the generic endpoint, and with both empty span export is disabled without affecting serving |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | Generic OTLP base URL; `/v1/traces` is appended |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `OTEL_EXPORTER_OTLP_PROTOCOL` | Must be `http/json` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/json` | Generic protocol; the only accepted value |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | `OTEL_EXPORTER_OTLP_HEADERS` | Comma-separated `key=value` headers sent with every export, percent-decoded |
| `OTEL_EXPORTER_OTLP_HEADERS` | empty | Generic form of the same |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `10` | Maximum seconds for one background OTLP batch export; never spent on a request thread |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `2048` | Bounded completed-span queue; overflow drops spans and increments `sandbox_trace_export_drops_total{reason="queue_full"}` |
| `OTEL_SERVICE_NAME` | `sandbox-control-plane` | Stable service identity attached to exported spans; the volume role should use `sandbox-volume` |
| `OTEL_SERVICE_VERSION` | `unknown` | `service.version` resource attribute on exported spans |

### Grafana embedding

Optional. The Console embeds one Grafana dashboard through a Control Plane
proxy; without `SANDBOX_GRAFANA_URL`, a token and a datasource uid the
integration is off and the Console says so. Details and the required Grafana
permissions are in [observability/README.md](../observability/README.md).

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_GRAFANA_URL` | empty | `http(s)://` origin of Grafana; credentials, query or fragment in the value disable it |
| `SANDBOX_GRAFANA_TOKEN` | empty | Viewer service-account token |
| `SANDBOX_GRAFANA_TOKEN_FILE` | empty | File to read the token from, used when the direct value is unset; a missing file means "not configured" |
| `SANDBOX_GRAFANA_DATASOURCE_UID` | empty | Datasource the panels query; required for the embed |
| `SANDBOX_GRAFANA_DASHBOARD_UID` | `sandbox-control-plane` | Dashboard uid; an invalid value falls back to the default |
| `SANDBOX_GRAFANA_ORG_ID` | `1` | Grafana organisation id |

## Control-plane database

These apply when `SANDBOX_STORE_BACKEND` is `postgresql` or `mysql`. Both drivers
(`psycopg`, `PyMySQL`) are pinned in `control_plane/requirements.lock` and imported during
the image build; a Control Plane whose image lacks the selected driver exits at startup
with the package name instead of serving `503`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_DB_HOST` | `sandbox-postgres` | Database host |
| `SANDBOX_DB_PORT` | `5432` (`3306` for `mysql`) | Database port |
| `SANDBOX_DB_NAME` | `sandbox` | Database name |
| `SANDBOX_DB_USER` | `sandbox` | Database user |
| `SANDBOX_DB_PASSWORD_FILE` | `/var/run/sandbox-db/password` | File mounted from a Secret; the password is never read from an environment variable |
| `SANDBOX_DB_CONNECT_TIMEOUT` | `5` | Seconds to wait for a connection |

The MySQL backend renders the shared schema with `VARCHAR` columns in place of
`TEXT` and requires MySQL 8.0 or later (`utf8mb4`, `UTC_TIMESTAMP(6)`).
`overlays/local` is the reference MySQL deployment.

Changing capacity limits must be reviewed together with namespace quota, Pod
requests, and storage behavior.

## Runtime settings

Read by `runtime/runtime_server.py` inside every sandbox Pod. The Control Plane
sets the identity and key values on the Pod it creates (`control_plane/manifests.py`);
an operator changes only the listen and session limits, through the template.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_HOST` | `0.0.0.0` | Listen address |
| `SANDBOX_PORT` | `8080` | Listen port |
| `SANDBOX_ID` | set by the Control Plane | Runtime id (`sb-...`) |
| `WORKSPACE_ID` | set by the Control Plane | Workspace id (`ws-...`) the Runtime is attached to |
| `SANDBOX_CAPABILITY_KEY` | set by the Control Plane | Per-instance verification key derived from `SIGNING_KEY` and the epoch; never a credential a caller sends |
| `SANDBOX_CAPABILITY_EPOCH` | `1` | Epoch the key was derived under; bumping it invalidates keys read out of older containers |
| `SANDBOX_WORKSPACE` | `/workspace` | Mount point of the Workspace inside the Pod |
| `SANDBOX_MAX_SHELL_SESSIONS` | `16` | Persistent shell sessions per Runtime; must be greater than zero |
| `SANDBOX_SHELL_SESSION_IDLE_TTL_SECONDS` | `1800` | A shell session idle longer than this is closed |
| `SANDBOX_SHELL_SESSION_MAX_WALL_SECONDS` | `3600` | Absolute lifetime of a shell session |

## file-service settings

Read by `file-service/file_service.py`, which serves workspace file operations
beside the Runtime. The Runtime process fills in the `WORKSPACE_*` and
`FILE_SERVICE_*` identity values from its own `SANDBOX_*` ones when they are
unset, so a Pod configures them once.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FILE_SERVICE_HOST` | `0.0.0.0` | Listen address |
| `FILE_SERVICE_PORT` | `8081` | Listen port |
| `WORKSPACE_CAPABILITY_KEY` | `SANDBOX_CAPABILITY_KEY` | Verification key for workspace-scoped tickets |
| `FILE_SERVICE_CAPABILITY_KEY` | `WORKSPACE_CAPABILITY_KEY` | Older name of the same key, still honoured when the newer one is unset |
| `WORKSPACE_CAPABILITY_EPOCH` | `1` | Epoch of that key |
| `WORKSPACE_ID` | set by the Control Plane | Workspace id served |
| `FILE_SERVICE_WORKSPACE` | `/workspace` | Workspace mount point |
| `MAX_CHECKPOINT_BYTES` | `67108864` (64 MiB) | Largest checkpoint archive written |
| `MAX_CHECKPOINT_SOURCE_BYTES` | `268435456` (256 MiB) | Largest workspace a checkpoint may be taken from |
| `MAX_CHECKPOINT_ENTRIES` | `20000` | Entry cap of a checkpoint archive |
| `MAX_BUNDLE_ENTRIES` | `5000` | Entry cap of a delivery bundle, deliberately tighter than a checkpoint |

### Workspace garbage collection

`k8s/workspace-gc.yaml` runs `file-service/gc_workspaces.py` as a CronJob on
the shared volume.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_GC_ROOT` | `/workspaces` | Volume root the job scans |
| `WORKSPACE_DATA_TTL_SECONDS` | `2592000` (30 days) | Workspace directories untouched for longer are deleted |
| `WORKSPACE_GC_DRY_RUN` | `false` | `1`, `true` or `yes` reports candidates without deleting |

## Client-side settings

Read by the SDK (`sandbox_platform`), `sandboxctl`, the MCP server and the
benchmark runner, on the caller's machine rather than in the cluster.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_CONTROL_PLANE_URL` | `http://127.0.0.1:18080` (no default in `bench/runner.py`) | Control Plane base URL |
| `SANDBOX_TOKEN` | required | API key or break-glass token presented to the Control Plane |
| `SANDBOX_SESSION_ID` | required for the MCP server | Any stable string; it selects the Workspace the MCP session works in, so the same value always lands in the same Workspace |
| `SANDBOX_LIFECYCLE_REFRESH_SECONDS` | `60` | Interval at which the client refreshes the Runtime lifecycle; values below `5` are raised to `5` |
| `SANDBOX_KUBE_CONTEXT` | empty | `bench/runner.py` only: kubeconfig context for the cluster-side measurements |

## Startup behavior

Required configuration is validated before serving traffic. Invalid storage modes,
template JSON, image references, or missing secrets fail startup. Once a database
backend is configured, connection or schema-migration failure also fails startup.
`/healthz` continues checking that database and its canonical schema, while
`/readyz` remains replica-local so a shared dependency outage does not remove the
only control-plane endpoint. An unconfigured database remains the explicit legacy
single-tenant mode.
