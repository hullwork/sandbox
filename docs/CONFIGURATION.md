# Configuration reference

Configuration is supplied through Kubernetes ConfigMaps and Secrets. Never put
credentials in Git, command history, the operator console, or workspace volumes.

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

🔴 **`SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED=false` does not close every non-OIDC way in.**
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
| `SANDBOX_MAX_MC_QUEUE` | `32` | Requests allowed to wait for the object-store client slot; beyond it Control Plane answers `503` at once |
| `SANDBOX_STORE_GAUGE_TTL_SECONDS` | `10` | Cache lifetime of the store-backed `/metrics` gauges, so scrapes do not each query the database |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_DRAIN_SECONDS` | `5` | After SIGTERM, seconds `/readyz` reports `503` before listening stops, so Endpoints drop the Pod first |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_INFLIGHT_SECONDS` | `120` | Seconds to wait for in-flight requests and background Runtime creation after listening stops |
| `SANDBOX_CONTROL_PLANE_SHUTDOWN_REAPER_SECONDS` | `60` | Seconds to wait for the reaper to finish its current round; the sum of the three shutdown values must stay below `terminationGracePeriodSeconds` |
| `OBJECT_STORE_CLIENT` | `/usr/local/bin/mc` | Path of the MinIO Client binary used for object-store operations (any S3-compatible endpoint) |
| `OBJECT_STORE_SIGNATURE_VERSION` | `S3v4` | Request signing version, `S3v2` or `S3v4` |
| `OBJECT_STORE_ADDRESSING_STYLE` | `auto` | Bucket addressing, `auto`, `virtual`, or `path` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | empty | Optional full OTLP/HTTP JSON `/v1/traces` endpoint; empty disables span export without affecting serving |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `10` | Maximum seconds for one background OTLP batch export; never spent on a request thread |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `2048` | Bounded completed-span queue; overflow drops spans and increments `sandbox_trace_export_drops_total{reason="queue_full"}` |
| `OTEL_SERVICE_NAME` | `sandbox-control-plane` | Stable service identity attached to exported spans; the volume role should use `sandbox-volume` |

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

Object-store endpoint, bucket, access key, secret key, and TLS settings are declared
in `k8s/object-store.yaml` and consumed through the `OBJECT_STORE_*` names. See source and manifests
for lower-level tuning variables; changing capacity limits must be reviewed together
with namespace quota, Pod requests, and storage behavior.

## MCP server settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_SESSION_ID` | required | Any stable string; it selects the Workspace the MCP session works in, so the same value always lands in the same Workspace |
| `SANDBOX_LIFECYCLE_REFRESH_SECONDS` | `60` | Interval at which the client refreshes the Runtime lifecycle; values below `5` are raised to `5` |

## Startup behavior

Required configuration is validated before serving traffic. Invalid storage modes,
template JSON, image references, or missing secrets fail startup. Once a database
backend is configured, connection or schema-migration failure also fails startup.
`/healthz` continues checking that database and its canonical schema, while
`/readyz` remains replica-local so a shared dependency outage does not remove the
only control-plane endpoint. An unconfigured database remains the explicit legacy
single-tenant mode.
