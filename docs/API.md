# HTTP and SDK contract

The route/authentication manifest is
[`../contracts/control-plane-openapi.yaml`](../contracts/control-plane-openapi.yaml). It is checked
against Control Plane routing three ways in CI: every `path ==` / `match_path` literal in
`control_plane/api.py`, every OpenAPI `paths` entry, and every row of `control plane.ROUTE_AUTH`
must be the same set (`tests/test_route_completeness.py`); every protected row must
answer 401 without credentials (`tests/test_openapi_contract.py`); and tenant
ownership is exercised against a SQLite-backed Control Plane
(`tests/test_api_authorization.py`). Named response schemas are closed
(`additionalProperties: false`) wherever the Control Plane emits a fixed field set;
proxied file-service, object-store, and MCP payloads deliberately permit extension
fields, and source and SDK tests remain the behavioral authority for those
provider-dependent fields.

## Terminology

| Term | Meaning |
| --- | --- |
| Workspace (`ws-` id) | Durable per-session directory on the shared volume; owned by one tenant |
| `sandbox` (HTTP `sb-` id) | A **Runtime**: one gVisor Pod attached to a Workspace, released on idle |
| SDK `Sandbox` | Facade over a **named Workspace**; `Sandbox.stop()` releases the Runtime, files persist |
| `session_id` | Caller-supplied name that derives the Workspace id (HMAC, never enumerable) |

Who may call what, how credentials are obtained and rotated, and which parts of
that are stable, are in the [authentication contract](AUTH.md). The table below
is the short form.

| Credential | Scope |
| --- | --- |
| Control Plane/tenant bearer token | Control-plane operations available to the configured identity |
| Scoped runtime token | One runtime/workspace boundary and a short lifetime (`access_token_expires_in` seconds) |
| Object ticket | Single-use, object-bound upload or download (`expires_in` seconds) |
| Admin key | Tenant, key, template, audit, and other administrative operations |

## Request tracing

Every response carries `X-Request-Id`, and it is the same value the Control Plane logs
as `trace_id` for that request. Quote it in a bug report and the matching log
line can be found directly.

Control Plane takes the trace id from the first of these that is usable:

| Source | Rule |
| --- | --- |
| `traceparent` request header | W3C trace context, version `00` only: `00-<32 hex>-<16 hex>-<2 hex>`. All-zero trace or span ids are invalid and treated as absent |
| `X-Request-Id` request header | Derived deterministically: `sha256(<value> as UTF-8)[:16].hex()`, so the same request id yields the same trace id in every service that sees it |
| neither | A fresh random trace id |

Calls Control Plane makes onward carry `traceparent` with the same trace id and a new
span id per hop. The **trace flags are inherited unchanged** from an inbound
header: they express the caller's sampling decision, and a hop that overwrote
them would be reversing a decision someone else made, invisibly. Control Plane chooses
flags (`01`) only for a trace it starts itself, where no decision exists yet.

🔴 A malformed `traceparent` never fails a request. It is treated as absent and
the next source applies. Tracing is a diagnostic aid; a request that failed
because its trace header was wrong would make the diagnostic layer an outage
source.

**Header-name casing is not part of this contract.** HTTP field names are
case-insensitive (RFC 9110), and each service sends whatever its HTTP client
produces - Control Plane's outbound calls go out as `Traceparent`, because that is what
the Python standard library emits. What *is* part of the contract is the other
half: **a receiver must match the name case-insensitively.** If you are
comparing a packet capture against the lowercase spelling used in the
specification, this is why they differ, and it is not a defect.

Known gap: object-storage traffic leaves through `boto3`, which
does not yet propagate `traceparent`. That hop is not traced yet, so a trace
stops at the object-store boundary rather than continuing through it.

## Routes

| Method | Path | Credential | Notes |
| --- | --- | --- | --- |
| GET | `/livez`, `/readyz`, `/healthz`, `/metrics` | none | Probes and metrics |
| GET | `/v1/auth/methods` | none | Which sign-in methods this deployment offers |
| GET | `/v1/auth/oidc/login` | none | Starts Authorization Code + PKCE at the configured provider |
| GET | `/v1/auth/oidc/callback` | none | Exchanges the code and mints the Console session |
| POST | `/v1/auth/logout` | browser session or control-plane | Clears session cookies |
| GET | `/v1/whoami`, `/v1/templates` | control-plane | Identity and visible templates |
| GET, POST | `/v1/workspaces` | control-plane | List visible Workspaces / create (returns `WorkspaceLease`) |
| POST | `/v1/workspaces/resolve` | control-plane | Session to Workspace + ready Runtime, read-only |
| DELETE | `/v1/workspaces/{workspace_id}` | control-plane + ownership | Deletes the directory; `purge=true` also deletes checkpoints |
| GET, POST | `/v1/workspaces/{workspace_id}/checkpoints` | control-plane + ownership | List / create archive |
| DELETE | `/v1/workspaces/{workspace_id}/checkpoints/{checkpoint_id}` | control-plane + ownership | |
| POST | `/v1/workspaces/{workspace_id}/checkpoints/{checkpoint_id}/restore` | control-plane + ownership | |
| GET, POST | `/v1/sandboxes` | control-plane | List visible Runtimes / create (`SandboxLease`, 201 ready or 202 pending) |
| GET | `/v1/monitoring` | control-plane | Node and Runtime resource snapshot |
| GET, DELETE | `/v1/sandboxes/{sandbox_id}` | control-plane + ownership | Runtime state / release |
| POST | `/v1/sandboxes/{sandbox_id}/token` | control-plane + ownership | Issue `ScopedToken` |
| POST | `/v1/sandboxes/{sandbox_id}/mcp` | scoped token | MCP JSON-RPC / SSE proxy to the Runtime |
| GET | `/v1/workspaces/{workspace_id}/files/{list,read,read-binary,glob,grep}` | scoped token, or control-plane + ownership | Proxied to the Runtime file service; 409 without a running Runtime |
| POST | `/v1/workspaces/{workspace_id}/files/{write,write-binary,edit}` | scoped token only | Same proxy, write subset |
| POST | `/v1/workspaces/{workspace_id}/objects/{import,export}` | scoped token only | Object store to/from workspace |
| GET, PUT | `/v1/storage/content` | object ticket | Raw object bytes |
| GET, POST, DELETE | `/v1/storage/objects` | control-plane + derived owner | Object record / put / delete |
| GET | `/v1/storage/objects/{list,stat,versions}` | control-plane + derived owner | |
| POST | `/v1/storage/tickets` | control-plane + derived owner | Issue `ObjectTicket` |
| GET, POST | `/v1/admin/tenants` | admin key | |
| DELETE | `/v1/admin/tenants/{tenant_id}` | admin key | Suspends, does not delete data |
| POST | `/v1/admin/tenants/{tenant_id}/status` | admin key | `active` / `suspended` |
| GET, POST | `/v1/admin/tenants/{tenant_id}/keys` | admin key | Tenant keys (`IssuedApiKey`, plaintext shown once) |
| GET, POST | `/v1/admin/tenants/{tenant_id}/owner-tenants` | admin key | Object owner prefixes a tenant may act for |
| DELETE | `/v1/admin/tenants/{tenant_id}/owner-tenants/{owner_tenant_id}` | admin key | |
| GET, POST | `/v1/admin/keys` | admin key | Admin keys |
| DELETE | `/v1/admin/keys/{key_id}` | admin key | |
| GET | `/v1/admin/audit` | admin key | Newest first, `limit` up to 1000 |
| GET, POST | `/v1/admin/templates` | admin key | Requires `SANDBOX_IMAGE_REGISTRIES` |
| DELETE | `/v1/admin/templates/{template_id}` | admin key | |

Ownership failures on by-id routes are reported as **404**, not 403, so that a
guessed id cannot be confirmed to exist; every denial is written to the audit log
(`workspace.access` / `sandbox.access`, `outcome=denied`).

`POST /v1/workspaces/resolve` performs an authenticated, read-only lookup from a
session identity to its Workspace and current ready Runtime. It neither creates a
Workspace nor exposes session identities through the Workspace list.

`GET /v1/monitoring` returns a current operational snapshot. Global identities see
node health/capacity and all Runtimes; tenant-scoped identities see only Runtimes
owned through their Workspaces and never receive node names or node inventory. CPU
values are normalized to millicores and memory to bytes. Actual usage comes from
`metrics.k8s.io`; when that API is absent or unhealthy, usage fields are `null` and
the response includes a stable availability reason while core health,
requests/limits, and node capacity remain available.

## Error model

Every non-2xx JSON body is `{"error": str}` with two optional extensions:
`retry_after_seconds` (integer) on the object-store back-pressure 503, and `hint`
on the 409 that file routes return when no Runtime serves the Workspace. There is
no machine-readable error code; the status code carries the meaning.

| Status | Meaning | Retry? |
| --- | --- | --- |
| 400 | Request rejected by validation (also raised for `ValueError`/`RuntimeError` inside handlers) | No, fix the request |
| 401 | Missing, invalid, expired, or retired credential | No, re-authenticate |
| 403 | Authenticated but not permitted (admin-only route, suspended tenant, a tenant credential selecting a tenant or an object owner) | No |
| 404 | Not found **or not owned by this tenant** | No |
| 409 | State conflict: Runtime still attached, template management disabled, no running Runtime for a file route | After changing state |
| 429 | Tenant or global capacity reached (`max_workspaces`, `max_runtimes`, `MAX_WORKSPACES`) | After releasing capacity |
| 503 | Control-plane store or Kubernetes unavailable, Control Plane shutting down, or object-store queue full (`retry_after_seconds`) | Yes, with backoff |
| 504 | A downstream call (Runtime, volume agent) exceeded the Control Plane deadline | Only for idempotent operations |

## SDK surface

`sandbox_platform.sandbox_client.Sandbox` is the user-facing named Workspace facade:

- Lifecycle: `create`, `get`, `get_or_create`, `status`, `stop`
- Execution: `run_command` returning `CommandResult`
- Files: `read_file`, `write_file`, `write_files`
- Persistence: `checkpoint` (a Workspace archive, not a VM snapshot)

`sandbox_platform.sandbox_client.SandboxManager` is the lower-level reference Control Plane client. Important groups:

- Connectivity: `ping`, `status`
- Workspace/runtime: `ensure_workspace`, `ensure_runtime`, `release_runtime`
- Files: `read_file`, `write_file`, `edit_file`, `glob_files`, `grep_files`
- Shell: `shell`, `shell_stream`, `shell_session`
- Objects: ticket, upload/download, stat/list/delete, workspace import/export
- Checkpoints: create, list, restore, delete

### Acting for an end user

A client that serves several end users binds the pseudonym for each around the
work it does on their behalf:

```python
with sandbox_client.acting_subject_context(pseudonym):
    sandbox_client.MANAGER.put_agent_blob(agent_id, run_id, path, data)
```

`pseudonym` is 32 lowercase hex characters, derived by you - see
[authentication](AUTH.md#3-acting-for-a-subject) for the derivation and the
published vectors. This client never derives one and never re-hashes the one it
is given: a second hash produces another perfectly valid pseudonym, and the
person you named and the person the platform records then differ while both
sides answer 2xx.

Two properties worth knowing before you wire it in:

- it is **ambient**, not a parameter. `SandboxManager` is a process-wide
  singleton, so an identity held on it would be shared by every request in
  flight in every thread; and a per-call argument is one more thing each call
  site can omit, silently. `acting_subject_context` binds a context variable,
  which is per task and per thread, and `_request` is the single place it is
  read;
- an object call inside no such scope **raises before it is sent**, naming the
  operation. The platform would answer `400` for the same reason, but that
  answer arrives with no way back to the call site that failed to bind. The one
  exception is a management-plane credential naming an `owner` outright, which
  needs no subject to build a partition from.

### Blocking, idempotency, and retry

The SDK performs **no retries and no backoff** of its own. Per ADR 0001, callers
may retry bounded, idempotent operations but must never substitute local
execution. Blocking upper bounds are the client socket timeouts in
`sandbox_platform.sandbox_client` (`SandboxManager._request` defaults to 100 s); the Control Plane-side
budget is noted where it differs.

| SDK method | Control Plane route | Blocks at most | Idempotent | Safe to retry |
| --- | --- | --- | --- | --- |
| `ping` | `GET /healthz` | 5 s | yes | yes |
| `ensure_workspace` | `POST /v1/workspaces` | 100 s | yes (same `session_id` derives the same `ws-` id) | yes |
| `ensure_runtime` | `POST /v1/sandboxes` (`wait=true`) | 100 s client; Control Plane waits up to 90 s for the Pod plus 20 s of health probing (110 s) | **no** (each call may create a new Runtime and consume quota) | only after `lookup_runtime` confirms none exists |
| `lookup_runtime` / `resolve_workspace` | `POST /v1/workspaces/resolve` + `POST /v1/sandboxes/{id}/token` | 100 s each | yes | yes |
| `release_runtime`, `Sandbox.stop` | `DELETE /v1/sandboxes/{id}` | 100 s | yes (the SDK returns `released: false` locally once cleared) | yes |
| `read_file`, `glob_files`, `grep_files` | MCP `tools/call` | 45 s (30 s tool timeout + 15 s) | yes | yes |
| `write_file` | MCP `tools/call` | 45 s | yes (same content) | yes |
| `edit_file` | MCP `tools/call` | 45 s | **no** (`old` must still be present) | only after re-reading the file |
| `shell`, `Sandbox.run_command` | MCP `tools/call` | `timeout_seconds` + 15 s (default 45 s; Runtime caps exec at 30 s) | **no** | no; the command may have run |
| `shell_stream` | MCP `tools/call` (SSE) | `timeout_seconds` + 15 s | **no** | no |
| `shell_session` | MCP `tools/call` (PTY session) | per call `timeout_seconds` + 15 s; session idle TTL 1800 s | **no** | no |
| `checkpoint_workspace`, `Sandbox.checkpoint` | `POST .../checkpoints` | 150 s | no (each call creates a new `checkpoint_id`) | yes (extra archives only cost storage) |
| `restore_workspace` | `POST .../checkpoints/{id}/restore` | 150 s | yes | yes |
| `list_workspace_checkpoints`, `delete_workspace_checkpoint` | `GET` / `DELETE .../checkpoints[/{id}]` | 100 s | yes | yes |
| `put_agent_blob` | `POST /v1/storage/tickets` + `PUT /v1/storage/content` | 100 s + 120 s | yes (versioned bucket keeps history) | yes with a fresh ticket |
| `open_object` | `POST /v1/storage/tickets` + `GET /v1/storage/content` | 100 s + 30 s | yes | yes with a fresh ticket |
| `stat_object`, `list_objects`, `delete_object` | `GET` / `DELETE /v1/storage/objects*` | 100 s | yes | yes |
| `import_object_to_workspace`, `export_workspace_object` | `POST .../objects/{import,export}` | 100 s | yes (same source and destination) | yes |

A `ControlPlaneError.status` below 500 comes from the Control Plane; 502 is synthesized by
the SDK for transport failures and malformed responses, and 404/409 can also be
synthesized by `lookup_runtime` and template mismatches.

The SDK fails closed when Control Plane is unavailable. It does not expose a local shell or local filesystem fallback.
