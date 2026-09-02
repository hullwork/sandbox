/**
 * In-memory fake backend that replaces the API layer when `VITE_USE_MOCK=1`.
 *
 * `/v1/whoami` and template endpoints were implemented in parallel, so the
 * multi-identity UI needed local testing before either a complete API or real
 * cluster was available.
 * Build data from the contract's response shapes and support writes; do not
 * simulate rate limits or cluster failures.
 * The “credentials” here are literals for switching identities and are not valid
 * secrets. Fake keys are random and no realistic constant is allowed: anything
 * credential-shaped in a production bundle is treated as a leak.
 * Data deliberately includes unpleasant edges: suspended tenants, revoked keys,
 * an admin without `templates:write` because no allowlist is configured, the
 * break-glass static token, and angle brackets in externally controlled text.
 * Comfortable fake data proves none of these guards.
 *
 * Identity switching: enter `admin`, `tenant`, `breakglass`, or `nowhitelist` on
 * the login page; every other value returns 401.
 */

// AI-LOCK: Only pure literals may appear at this file's top level. Property
// accesses, function calls, and spreads can be treated as side effects and retain
// fake data in production. This happened in practice: `let identity =
// WHOAMI.admin` kept WHOAMI in dist. The top-level reference has since been
// removed and bundle scans found no hits. Never add real or realistic
// credentials, tenant names, or image registries here; a real token in dist would
// be delivered to every visitor. Rescan bundles after changing top-level code.

import { ControlPlaneError } from "./api-contract";
import { loadToken } from "./auth";
import type { ControlPlaneApi, TemplateInput, TenantInput } from "./api-contract";
import type {
  ApiKeyView,
  IssuedKeyView,
  MonitoringView,
  TemplateRecordView,
  TenantView,
  WhoamiView,
} from "./types";

const NOW = Date.now();

/**
 * Database timestamps. Both backend shapes must appear:
 *   - SQLite: `2026-08-15 02:35:22`, UTC without a timezone marker.
 *   - PostgreSQL: `2026-08-15T02:30:00+00:00` after store-side isoformat.
 * Generating only one shape would leave formatDbTime's naive-UTC branch untested.
 */
function dbTime(minutesAgo: number, iso = false): string {
  const at = new Date(NOW - minutesAgo * 60_000);
  return iso
    ? at.toISOString().replace("Z", "+00:00")
    : at.toISOString().replace("T", " ").slice(0, 19);
}

function unixSeconds(minutesFromNow: number): string {
  return String(Math.floor(NOW / 1000 + minutesFromNow * 60));
}

/** Fake one-time key. The real value is server-generated; this only needs shape and uniqueness. */
function fakeSecret(prefix: string): string {
  const random = Array.from({ length: 4 }, () =>
    Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, "0"),
  ).join("");
  return `sk_${random.slice(0, 9)}_${prefix}_${random.slice(9)}`;
}

/** Capability lists copied from live Control Plane output rather than inferred from kind. */
const ADMIN_CAPABILITIES = [
  "keys:write",
  "monitoring:read",
  "nodes:read",
  "sandboxes:write",
  "templates:read",
  "templates:read:all",
  "templates:write",
  "tenants:write",
  "workspaces:read",
  "workspaces:read:all",
  "workspaces:write",
];

const TENANT_CAPABILITIES = [
  "monitoring:read",
  "sandboxes:write",
  "templates:read",
  "workspaces:read",
  "workspaces:write",
];

// Use satisfies rather than `: Record<string, WhoamiView>`; the latter would add
// undefined to WHOAMI.admin under noUncheckedIndexedAccess even though keys are fixed.
const WHOAMI = {
  admin: {
    kind: "admin",
    tenant_id: null,
    key_id: "0f1e2d3c4b5a6978",
    capabilities: ADMIN_CAPABILITIES,
  },
  // An admin without an image allowlist. Contract §2 fails closed and omits
  // templates:write, so the template page should show only a read-only list and
  // explanation. List capabilities literally; do not derive them with
  // ADMIN_CAPABILITIES.filter. Top-level expressions must remain pure literals:
  // iteration and function calls can preserve this object in production even when
  // mockApi itself is dead-code eliminated. Fake data may exist only in mock builds.
  nowhitelist: {
    kind: "admin",
    tenant_id: null,
    key_id: "0f1e2d3c4b5a6978",
    capabilities: [
      "keys:write",
      "sandboxes:write",
      "templates:read",
      "templates:read:all",
      "tenants:write",
      "workspaces:read",
      "workspaces:read:all",
      "workspaces:write",
    ],
  },
  tenant: {
    kind: "tenant",
    tenant_id: "acme",
    key_id: "aabbccddeeff0011",
    capabilities: TENANT_CAPABILITIES,
    tenant: {
      name: "Acme <studio>",
      status: "active",
      max_workspaces: 3,
      used_workspaces: 1,
    },
  },
  // Break-glass capabilities are exactly the same as admin capabilities; only
  // kind differs. Copy that behavior. A reduced list would hide regressions
  // where code distinguishes this identity by capability rather than kind.
  breakglass: {
    kind: "break-glass",
    tenant_id: null,
    key_id: null,
    capabilities: ADMIN_CAPABILITIES,
  },
} satisfies Record<string, WhoamiView>;

/**
 * Current fake identity.
 *
 * The initial value must be null, never `= WHOAMI.admin`. A top-level property
 * access is not provably pure (the property may be a getter), so the minifier may
 * retain WHOAMI even after eliminating mockApi method bodies. Fake data may exist
 * only in VITE_USE_MOCK=1 builds; defer property access, calls, and spreads to
 * function bodies.
 */
let identity: WhoamiView | null = null;

/** Default identity before login; the fake backend does not authenticate initial calls. */
function currentIdentity(): WhoamiView {
  return identity ?? WHOAMI.admin;
}

const tenants: TenantView[] = [
  {
    id: "local",
    display_name: "Local",
    status: "active",
    max_workspaces: 8,
    max_runtimes: 8,
    workspaces_in_use: 2,
  },
  {
    id: "acme",
    // Externally controlled text. Display names render as plain text, so angle
    // brackets must appear literally on the page.
    display_name: "Acme <studio>",
    status: "active",
    max_workspaces: 3,
    max_runtimes: 2,
    workspaces_in_use: 1,
  },
  {
    id: "suspended-co",
    display_name: "Suspended tenant",
    status: "suspended",
    max_workspaces: 1,
    max_runtimes: 1,
    workspaces_in_use: 4,
  },
];

const keys: ApiKeyView[] = [
  {
    id: "1122334455667788",
    tenant_id: "acme",
    key_prefix: "sk_R7gk3_ac",
    label: "CI",
    created_at: dbTime(60 * 24 * 9, true),
    last_used_at: dbTime(12),
    revoked_at: null,
  },
  // Issued but never called: NULL last_used_at differs from “last used long ago”.
  // Without this row, the never-used rendering branch remains untested.
  {
    id: "5566778899aabbcc",
    tenant_id: "acme",
    key_prefix: "sk_M2q8z_ac",
    label: "Issued to a contractor but never used",
    created_at: dbTime(60 * 24 * 2),
    last_used_at: null,
    revoked_at: null,
  },
  {
    id: "99aabbccddeeff00",
    tenant_id: "acme",
    key_prefix: "sk_Z5v4w_ac",
    label: "Old key before rotation",
    created_at: dbTime(60 * 24 * 40),
    last_used_at: dbTime(60 * 24 * 39),
    revoked_at: dbTime(60 * 24 * 38),
  },
];

const templates: TemplateRecordView[] = [
  {
    tenant_id: "*",
    template_id: "playwright",
    image: "ghcr.io/convee/sandbox-playwright:0.4.1",
    created_at: dbTime(60 * 24 * 3, true),
    created_by: "0f1e2d3c4b5a6978",
    allowed: true,
  },
  {
    tenant_id: "acme",
    template_id: "acme-etl",
    image: "registry.local/sandbox/acme-etl:2026-08-01",
    created_at: dbTime(90),
    created_by: "0f1e2d3c4b5a6978",
    // Deliberately outside the allowlist. This state occurs after an allowlist is
    // tightened; without a row, the outside-allowlist rendering branch is untested.
    allowed: false,
  },
];

/** Built-in templates from SANDBOX_TEMPLATES; database records cannot override them. */
const BUILTIN_TEMPLATE_IDS = ["default", "python"];

function requireCapability(capability: string): void {
  if (!currentIdentity().capabilities.includes(capability)) {
    throw new ControlPlaneError(403, `mock: current identity lacks ${capability}`, null);
  }
}

export const mockApi: ControlPlaneApi = {
  async authMethods() {
    // Both on, so the login screen renders every branch it has.
    return { local_login: true, oidc: true };
  },

  async logout() {},
  async whoami(tokenOverride) {
    // No override means a post-refresh recheck. Read sessionStorage so this path
    // matches the live implementation. Do not reuse the in-memory identity: module
    // state resets on refresh, which could upgrade a tenant to admin and hide the
    // most important behavior.
    const candidate = (tokenOverride ?? loadToken()).trim();
    const found: WhoamiView | undefined =
      WHOAMI[candidate as keyof typeof WHOAMI];
    if (!found) {
      throw new ControlPlaneError(401, "unauthorized", { error: "unauthorized" });
    }
    identity = found;
    return found;
  },

  async getHealth() {
    return { status: "ok", kubernetes: "ok", object_storage: "unchecked" };
  },

  async getMonitoring(): Promise<MonitoringView> {
    const nodesVisible = currentIdentity().capabilities.includes("nodes:read");
    return {
      scope: nodesVisible ? "cluster" : "tenant",
      nodes_visible: nodesVisible,
      metrics: {
        nodes: nodesVisible ? { available: true, reason: null } : null,
        runtimes: { available: true, reason: null },
      },
      nodes: nodesVisible ? [{
        name: "sandbox-node-1",
        status: "ready",
        roles: ["runtime"],
        unschedulable: false,
        cpu: { usage_millicores: 640, allocatable_millicores: 4000, capacity_millicores: 4000 },
        memory: { usage_bytes: 1_825_361_920, allocatable_bytes: 8_053_063_680, capacity_bytes: 8_589_934_592 },
        pod_capacity: 110,
        kubelet_version: "v1.36.4",
        os_image: "Ubuntu 24.04 LTS",
        architecture: "arm64",
      }] : [],
      runtimes: [{
        id: "sb-0123456789ab",
        workspace_id: "ws-0123456789ab",
        status: "running",
        runtime_class: "gvisor",
        template: "default",
        created_at: unixSeconds(-30),
        expires_at: unixSeconds(30),
        node: nodesVisible ? "sandbox-node-1" : null,
        ready: true,
        restarts: 0,
        cpu: { usage_millicores: 95, request_millicores: 250, limit_millicores: 1000 },
        memory: { usage_bytes: 201_326_592, request_bytes: 268_435_456, limit_bytes: 1_073_741_824 },
      }],
    };
  },

  async listSandboxes() {
    return [
      {
        id: "sb-0123456789ab",
        workspace_id: "ws-0123456789ab",
        status: "Running",
        runtime_class: "gvisor",
        template: "default",
        created_at: unixSeconds(-30),
        expires_at: unixSeconds(30),
      },
    ];
  },

  async listWorkspaces() {
    return [
      {
        id: "ws-0123456789ab",
        status: "active",
        created_at: unixSeconds(-600),
        last_used_at: unixSeconds(-2),
        runtime_attached: true,
        idle_expires_at: unixSeconds(358),
      },
    ];
  },

  async deleteSandbox() {},
  async deleteWorkspace() {},

  async listWorkspaceFiles(id, path) {
    return {
      workspace_id: id,
      path,
      entries: [
        { name: "notes", type: "directory" },
        { name: "README.md", type: "file" },
      ],
      truncated: false,
    };
  },

  async readWorkspaceFile(id, path) {
    return {
      workspace_id: id,
      path,
      content: "# mock\nFile content in mock-data mode.\n",
      start_line: 1,
      end_line: 2,
      truncated: false,
    };
  },

  async listTenants() {
    requireCapability("tenants:write");
    return tenants.map((tenant) => ({ ...tenant }));
  },

  async createTenant(input: TenantInput) {
    requireCapability("tenants:write");
    if (tenants.some((tenant) => tenant.id === input.id)) {
      throw new ControlPlaneError(409, `tenant already exists: ${input.id}`, null);
    }
    tenants.push({
      id: input.id,
      display_name: input.display_name,
      status: "active",
      max_workspaces: input.max_workspaces,
      max_runtimes: input.max_runtimes,
      workspaces_in_use: 0,
    });
  },

  async setTenantStatus(id, status) {
    requireCapability("tenants:write");
    // Match Control Plane's error paths: invalid status returns 400 rather than 503
    // (which would suggest waiting for store recovery), and an unknown tenant
    // returns 404 rather than a silent 200 that misleadingly reports success.
    if (status !== "active" && status !== "suspended") {
      throw new ControlPlaneError(400, "status must be active or suspended", null);
    }
    const tenant = tenants.find((item) => item.id === id);
    if (!tenant) {
      throw new ControlPlaneError(404, `unknown tenant: ${id}`, null);
    }
    tenant.status = status;
  },

  async listApiKeys(tenantId) {
    requireCapability("tenants:write");
    return keys.filter((key) => key.tenant_id === tenantId).map((key) => ({ ...key }));
  },

  async issueApiKey(tenantId, label) {
    requireCapability("tenants:write");
    const issued: IssuedKeyView = {
      id: Math.floor(Math.random() * 0xffffffff).toString(16).padStart(16, "0"),
      tenant_id: tenantId,
      label,
      api_key: fakeSecret(tenantId),
      note: "api_key is shown once and cannot be retrieved later",
    };
    keys.push({
      id: issued.id,
      tenant_id: tenantId,
      key_prefix: issued.api_key.slice(0, 12),
      label,
      created_at: dbTime(0),
      // Just issued and not yet used.
      last_used_at: null,
      revoked_at: null,
    });
    return issued;
  },

  async revokeApiKey(keyId) {
    requireCapability("tenants:write");
    const key = keys.find((item) => item.id === keyId);
    if (key && !key.revoked_at) {
      key.revoked_at = dbTime(0);
    }
  },

  async listTemplateIds() {
    const visible = templates
      .filter(
        (template) =>
          template.tenant_id === "*"
          || template.tenant_id === currentIdentity().tenant_id,
      )
      .map((template) => template.template_id);
    return [...new Set([...BUILTIN_TEMPLATE_IDS, ...visible])].sort();
  },

  async listTemplateRecords() {
    requireCapability("templates:read:all");
    return templates.map((template) => ({ ...template }));
  },

  async createTemplate(input: TemplateInput) {
    // Follow contract validation order: allowlist before ID. The reverse would
    // reveal both invalid values only across two failed round trips.
    requireCapability("templates:write");
    const tenantId = input.tenant_id || "*";
    if (
      !["ghcr.io/convee/", "registry.local/sandbox/"].some((prefix) =>
        input.image.startsWith(prefix),
      )
    ) {
      throw new ControlPlaneError(409, `image is not in SANDBOX_IMAGE_REGISTRIES`, null);
    }
    if (!/^[a-z0-9][-a-z0-9]{0,31}$/.test(input.template_id)) {
      throw new ControlPlaneError(400, "template_id is invalid", null);
    }
    if (BUILTIN_TEMPLATE_IDS.includes(input.template_id)) {
      throw new ControlPlaneError(409, "builtin template cannot be overridden", null);
    }
    templates.push({
      tenant_id: tenantId,
      template_id: input.template_id,
      image: input.image,
      created_at: dbTime(0),
      created_by: currentIdentity().key_id,
      // A newly written row has necessarily passed allowlist validation.
      allowed: true,
    });
  },

  async deleteTemplate(templateId, tenantId) {
    requireCapability("templates:write");
    const index = templates.findIndex(
      (template) =>
        template.template_id === templateId && template.tenant_id === tenantId,
    );
    if (index >= 0) {
      templates.splice(index, 1);
    }
  },
};
