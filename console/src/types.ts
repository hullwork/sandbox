/**
 * Data contract for Sandbox Control Plane read views. Source: sandbox/control_plane/core.py.
 *
 * Map Control Plane JSON directly to TypeScript without adding defaults or converting
 * units. Field names match `sandbox_view` and `workspace_view` exactly. Timestamps
 * are strings containing Unix seconds taken from annotations—not numbers or ISO
 * strings—and format.ts centralizes formatting.
 */

export interface SandboxView {
  id: string | null;
  workspace_id: string | null;
  status: string;
  runtime_class: string | null;
  template: string;
  created_at: string | null;
  expires_at: string | null;
}

/**
 * AI-LOCK: Render `idle_expires_at` and `runtime_attached` together. Control Plane
 * reclaims only when no runtime is active and `last_used_at + IDLE_TTL` has
 * passed. A deadline alone would incorrectly imply overdue reclamation while a
 * runtime is intentionally retaining the workspace. See the matching lock in
 * core.py `workspace_view`; do not reduce the table to just one field.
 */
export interface WorkspaceView {
  id: string | null;
  status: string;
  created_at: string | null;
  last_used_at: string | null;
  runtime_attached: boolean;
  idle_expires_at: string | null;
}

/** Control Plane `/healthz`: 200 returns three status fields; 503 returns error fields. */
export interface HealthView {
  status?: string;
  kubernetes?: string;
  /** "ok" or "unchecked"; managed S3 may honestly report unchecked without an anonymous health endpoint. */
  object_storage?: string;
  error?: string;
  endpoint?: string;
  diagnosis?: string;
}

export interface MetricAvailability {
  available: boolean;
  reason: "metrics_api_unavailable" | "metrics_api_forbidden" | "metrics_api_error" | null;
}

export interface CpuMonitoring {
  usage_millicores: number | null;
  request_millicores?: number;
  limit_millicores?: number;
  allocatable_millicores?: number | null;
  capacity_millicores?: number | null;
}

export interface MemoryMonitoring {
  usage_bytes: number | null;
  request_bytes?: number;
  limit_bytes?: number;
  allocatable_bytes?: number | null;
  capacity_bytes?: number | null;
}

export interface NodeMonitoringView {
  name: string;
  status: "ready" | "not_ready";
  roles: string[];
  unschedulable: boolean;
  cpu: CpuMonitoring;
  memory: MemoryMonitoring;
  pod_capacity: number;
  kubelet_version: string | null;
  os_image: string | null;
  architecture: string | null;
}

export interface RuntimeMonitoringView extends SandboxView {
  node: string | null;
  ready: boolean;
  restarts: number;
  cpu: CpuMonitoring;
  memory: MemoryMonitoring;
}

export interface MonitoringView {
  scope: "cluster" | "tenant";
  nodes_visible: boolean;
  metrics: {
    nodes: MetricAvailability | null;
    runtimes: MetricAvailability;
  };
  nodes: NodeMonitoringView[];
  runtimes: RuntimeMonitoringView[];
}

/**
 * Data contract for files inside a workspace. Source: file-service/file_service.py.
 *
 * `type` is exactly "directory", "file", or "other" (not "dir"). Entries contain
 * names but not sizes because File Service intentionally avoids stat-ing every
 * child. Lists truncate at MAX_LIST_ENTRIES and expose `truncated`; never treat a
 * truncated response as the complete set.
 */
export interface WorkspaceFileEntry {
  name: string;
  type: "directory" | "file" | "other";
}

export interface WorkspaceListView {
  workspace_id: string;
  /** The root is "."; other values are relative to the workspace root. */
  path: string;
  entries: WorkspaceFileEntry[];
  truncated: boolean;
}

export interface WorkspaceReadView {
  workspace_id: string;
  path: string;
  content: string;
  start_line: number;
  end_line: number;
  truncated: boolean;
  /** Present only when truncated; page with this value rather than end_line + 1. */
  next_offset?: number;
  /** Present when a line exceeds budget and is hard-clipped; paging cannot recover it. */
  clipped_line?: number;
  clipped_length?: number;
}

/**
 * Data contract for `GET /v1/whoami`. Source: contract §1.
 *
 * The console must know the credential's identity and capabilities before
 * deciding which tabs to render. Without this endpoint it would probe admin APIs
 * and infer tenancy from 403 responses, turning every load into unauthorized
 * discovery traffic.
 *
 * `capabilities` is computed server-side; never infer it from `kind`. For
 * example, an admin receives no `templates:write` capability when no image
 * allowlist is configured (contract §2 fail-closed). Rendering by kind would
 * expose a write form guaranteed to return 409.
 */
export type WhoamiKind = "admin" | "tenant" | "break-glass";

export interface WhoamiTenant {
  name: string;
  status: string;
  max_workspaces: number;
  /** Same value as `workspaces_in_use` from `/v1/admin/tenants`, under another name. */
  used_workspaces: number;
}

export interface WhoamiView {
  kind: WhoamiKind;
  tenant_id: string | null;
  key_id: string | null;
  /** Pseudonymous subject this request acts for, when the caller named one. */
  acting_subject?: string | null;
  capabilities: string[];
  tenant?: WhoamiTenant | null;
  /**
   * Embedded observability panel, when this deployment has one.
   *
   * Absent for a tenant identity even when the panel exists: the metrics behind
   * it carry no tenant dimension, so every panel is cross-tenant data and the
   * whole feature is an operator view. Control Plane decides that, not this field -
   * hiding the tab is presentation, and `/grafana/*` re-checks on every request.
   */
  grafana?: GrafanaCapability | null;
}

/** One embeddable panel of the shipped dashboard. */
export interface GrafanaPanel {
  id: number;
  title: string;
}

/**
 * Data contract for the `grafana` block of `GET /v1/whoami`. Source:
 * control_plane/grafana_proxy.py::capabilities.
 *
 * `enabled: false` is the normal case for a deployment with no Grafana; the
 * console renders no tab and the repository stays deployable without one.
 */
export interface GrafanaCapability {
  enabled: boolean;
  /** Same-origin prefix Control Plane proxies panels on. Never a Grafana address. */
  route?: string;
  dashboardUid?: string;
  panels?: GrafanaPanel[];
}

/**
 * Which sign-in methods this deployment offers (`GET /v1/auth/methods`).
 *
 * `local_login` is the static break-glass token, not the API-key form: keys
 * issued by the control plane are revocable and attributable and stay usable
 * whatever the identity provider is doing. `oidc` decides whether the
 * single-sign-on button exists at all.
 */
export interface AuthMethodsView {
  local_login: boolean;
  oidc: boolean;
}

/**
 * Data contract for `GET /v1/admin/tenants`. Source: core.py.
 *
 * Field names match Control Plane exactly. `workspaces_in_use` is computed live through
 * STORE.count_workspaces and is not a persisted column, so it has no separate
 * “last measured” timestamp.
 */
export interface TenantView {
  id: string;
  display_name: string;
  /** "active" or "suspended". Suspension revokes credentials but leaves directories on disk. */
  status: string;
  max_workspaces: number;
  max_runtimes: number;
  workspaces_in_use: number;
}

/**
 * Data contract for `GET /v1/admin/tenants/{id}/keys`.
 *
 * Plaintext appears only in the issuance response; storage retains SHA-256 only.
 * `key_prefix` locates a row during authentication and is not part of the
 * credential, so it is safe to display—but it does not identify a key's purpose,
 * which is why `label` matters. Both backends return timestamp strings with
 * different shapes: SQLite returns `YYYY-MM-DD HH:MM:SS` as UTC without a zone,
 * while PostgreSQL returns offset-aware ISO strings. Do not slice by length;
 * format through formatDbTime.
 */
export interface ApiKeyView {
  id: string;
  tenant_id: string | null;
  key_prefix: string;
  label: string;
  created_at: string | null;
  last_used_at: string | null;
  /** Non-null when revoked. Revocation is idempotent and returns 200 again. */
  revoked_at: string | null;
}

/** Issuance response; `api_key` is the only plaintext appearance and must remain in React state. */
export interface IssuedKeyView {
  id: string;
  tenant_id: string;
  label: string;
  api_key: string;
  note?: string;
}

/**
 * Data contract for templates. Source: contract §2.
 *
 * The two endpoints intentionally have different shapes and types rather than an
 * optional image field:
 *   - `GET /v1/templates` returns only IDs because image addresses expose cluster
 *     topology.
 *   - `GET /v1/admin/templates` returns complete records including images.
 * Merging them would make an empty image column meaningful only after reading code.
 */
export interface TemplateRecordView {
  /** '*' means global and visible to every tenant. It replaces NULL because NULL
   *  in a PostgreSQL composite primary key fails to enforce uniqueness (contract §2). */
  tenant_id: string;
  template_id: string;
  image: string;
  created_at: string | null;
  /** Key ID that created this record, enabling attribution after an incident. */
  created_by: string | null;
  /** Whether this image currently falls inside the deployed registry allowlist.
   *  Tightening the allowlist immediately deactivates existing rows: Control Plane's
   *  available_templates skips them even though the records remain. Without this
   *  field, tenants would see a template vanish while administrators still see a
   *  seemingly healthy row. */
  allowed: boolean;
}
