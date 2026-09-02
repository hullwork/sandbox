/**
 * API layer: every Control Plane call made by Sandbox Console.
 *
 * This module maps Control Plane's HTTP contract to functions. It does not render or
 * cache responses.
 *
 * Security contract:
 *   The old design assumed one operator identity, so nginx could overwrite every
 *   request with `Authorization: Bearer <control-plane-token>` and the browser held no
 *   credential. The prior rule was therefore “never introduce a token here”.
 *   The console now serves both platform operators and tenants. A server-fixed
 *   token cannot express which identity is acting, and choosing either side would
 *   lock out the other or expose admin authority to everyone. The caller must
 *   supply the credential, and this module attaches it to requests.
 *
 *   The identity model changed, but these restrictions remain:
 *   - Never store credentials in localStorage, cookies, or IndexedDB. Use
 *     sessionStorage only; auth.ts is the single storage location (AI-LOCK).
 *   - Never place credentials in a URL query or fragment. URLs enter history,
 *     Referer headers, and server access logs.
 *   - Do not retain copies or log credentials here. Read the value for each
 *     request; DevTools output can be captured in screenshots or recordings.
 *   - Never deliver a workspace-scoped token to the browser. Ownerless tokens are
 *     administrator-equivalent for object storage; file browsing continues through
 *     Control Plane's read-only files route (`require_workspace_read_auth`).
 *   - Never derive permissions from `kind`. `/v1/whoami` capabilities are the
 *     authority for what the console may do.
 *
 * Downstream: sandbox-control-plane over HTTP.
 * Contract: scratchpad/contract.md §1/§2 and Control Plane's `/v1` routes.
 * Failures: every non-2xx response throws ControlPlaneError for local display. Do not
 * retry automatically; polling pages already refresh periodically and retries
 * double request volume during an outage.
 */

import { loadToken } from "./auth";
import { mockApi } from "./mock";
import {
  ControlPlaneError,
  type ControlPlaneApi,
} from "./api-contract";
export {
  ControlPlaneError,
  type ControlPlaneApi,
  type TemplateInput,
  type TenantInput,
  type TenantStatus,
} from "./api-contract";
import type {
  ApiKeyView,
  AuthMethodsView,
  HealthView,
  IssuedKeyView,
  MonitoringView,
  SandboxView,
  TemplateRecordView,
  TenantView,
  WhoamiView,
  WorkspaceListView,
  WorkspaceReadView,
  WorkspaceView,
} from "./types";

/**
 * The CSRF token that accompanies a Console session cookie.
 *
 * Two names because the `__Host-` prefix is only legal on a Secure cookie; a
 * loopback HTTP development origin gets the bare name, and the browser drops
 * the prefixed one silently rather than reporting anything.
 */
function consoleCsrf(): string {
  const names = ["__Host-sandbox_console_csrf=", "sandbox_console_csrf="];
  const parts = document.cookie.split("; ");
  for (const name of names) {
    const item = parts.find((part) => part.startsWith(name));
    if (item) {
      return decodeURIComponent(item.split("=", 2)[1] ?? "");
    }
  }
  return "";
}

/**
 * Resolve a relative path against the current origin without URL credentials.
 *
 * Operators sometimes bookmark or paste `http://user:pass@host/`. Fetch forbids
 * credentials in request URLs, while relative URLs inherit them from the page URL;
 * without this normalization every request would throw TypeError and leave only an
 * error banner. Removing username/password supports both entry styles. Without it,
 * the first screen fails with “Request cannot be constructed from a URL that
 * includes credentials”.
 */
function sameOriginUrl(path: string): string {
  const base = new URL(window.location.href);
  base.username = "";
  base.password = "";
  return new URL(path, base).toString();
}

/**
 * @param tokenOverride Login-only candidate credential that has not been stored.
 *        Validate before saving so pages do not each surface a separate 401 and
 *        make the console appear broken.
 */
async function request<T>(
  path: string,
  init?: RequestInit,
  tokenOverride?: string,
): Promise<T> {
  const token = tokenOverride ?? loadToken();
  let response: Response;
  try {
    response = await fetch(sameOriginUrl(path), {
      ...init,
      headers: {
        Accept: "application/json",
        // Do not send an empty Bearer header. Control Plane returns 401 either way, but an
        // empty value makes nginx logs look like a rejected credential.
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(!token && init?.method && init.method !== "GET"
          ? { "X-Console-CSRF": consoleCsrf() }
          : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    // fetch rejects only at the network layer. Keep the raw browser cause here;
    // UI callers attach the localized “cannot reach console” context.
    throw new ControlPlaneError(0, String(cause), null);
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : response.statusText;
    throw new ControlPlaneError(response.status, detail, body);
  }
  return body as T;
}

function jsonBody(payload: unknown): RequestInit {
  return {
    body: JSON.stringify(payload),
    headers: { "Content-Type": "application/json" },
  };
}

const liveApi: ControlPlaneApi = {
  authMethods() {
    // Deliberately unauthenticated: the login screen has to know which methods
    // exist before any credential is available.
    return request<AuthMethodsView>("/v1/auth/methods", undefined, "");
  },

  whoami(tokenOverride) {
    return request<WhoamiView>("/v1/whoami", undefined, tokenOverride);
  },

  async logout() {
    await request("/v1/auth/logout", { method: "POST" }, "");
  },

  /**
   * Control Plane health. Preserve bodies for both 200 and 503: the 503 body contains
   * endpoint and diagnosis details that are primary evidence for object-store
   * failures. Exclude 401, which describes credentials rather than storage and
   * would misdirect troubleshooting if displayed as health data.
   */
  async getHealth() {
    try {
      return await request<HealthView>("/healthz");
    } catch (error) {
      if (
        error instanceof ControlPlaneError
        && error.status !== 401
        && error.body
        && typeof error.body === "object"
      ) {
        return error.body as HealthView;
      }
      throw error;
    }
  },

  async listSandboxes() {
    const data = await request<{ sandboxes: SandboxView[] }>("/v1/sandboxes");
    return data.sandboxes ?? [];
  },

  getMonitoring() {
    return request<MonitoringView>("/v1/monitoring");
  },

  async listWorkspaces() {
    const data = await request<{ workspaces: WorkspaceView[] }>("/v1/workspaces");
    return data.workspaces ?? [];
  },

  async deleteSandbox(id) {
    await request(`/v1/sandboxes/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  async deleteWorkspace(id) {
    await request(`/v1/workspaces/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  listWorkspaceFiles(id, path) {
    const query = new URLSearchParams({ path });
    return request<WorkspaceListView>(
      `/v1/workspaces/${encodeURIComponent(id)}/files/list?${query}`,
    );
  },

  readWorkspaceFile(id, path, offset) {
    const query = new URLSearchParams({ path });
    if (offset !== undefined && offset > 1) {
      query.set("offset", String(offset));
    }
    return request<WorkspaceReadView>(
      `/v1/workspaces/${encodeURIComponent(id)}/files/read?${query}`,
    );
  },

  async listTenants() {
    const data = await request<{ tenants: TenantView[] }>("/v1/admin/tenants");
    return data.tenants ?? [];
  },

  async createTenant(input) {
    await request("/v1/admin/tenants", { method: "POST", ...jsonBody(input) });
  },

  /**
   * Suspension and restoration share one route.
   *
   * Suspension is not deletion: tenant workspaces remain on disk, and deleting
   * records would orphan those directories (see core.py). The UI must say
   * “suspend” accurately.
   * `DELETE /v1/admin/tenants/{id}` also suspends and remains public for backward
   * compatibility, but this console intentionally uses the shared status endpoint.
   * Both directions then share one error contract (400 = invalid status,
   * 404 = missing tenant).
   */
  async setTenantStatus(id, status) {
    await request(`/v1/admin/tenants/${encodeURIComponent(id)}/status`, {
      method: "POST",
      ...jsonBody({ status }),
    });
  },

  async listApiKeys(tenantId) {
    const data = await request<{ keys: ApiKeyView[] }>(
      `/v1/admin/tenants/${encodeURIComponent(tenantId)}/keys`,
    );
    return data.keys ?? [];
  },

  issueApiKey(tenantId, label) {
    return request<IssuedKeyView>(
      `/v1/admin/tenants/${encodeURIComponent(tenantId)}/keys`,
      { method: "POST", ...jsonBody({ label }) },
    );
  },

  async revokeApiKey(keyId) {
    await request(`/v1/admin/keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
  },

  async listTemplateIds() {
    const data = await request<{ templates: string[] }>("/v1/templates");
    return data.templates ?? [];
  },

  async listTemplateRecords() {
    const data = await request<{ templates: TemplateRecordView[] }>(
      "/v1/admin/templates",
    );
    return data.templates ?? [];
  },

  async createTemplate(input) {
    await request("/v1/admin/templates", { method: "POST", ...jsonBody(input) });
  },

  async deleteTemplate(templateId, tenantId) {
    const query = new URLSearchParams({ tenant_id: tenantId });
    await request(
      `/v1/admin/templates/${encodeURIComponent(templateId)}?${query}`,
      { method: "DELETE" },
    );
  },
};

// Control Plane's `/v1/whoami` and template endpoints were implemented in parallel.
// The UI must remain runnable and screenshotable before they are available. The
// switch is a build-time constant; when `VITE_USE_MOCK` is not "1", `mockApi` is
// unreachable and production code never calls it.
//
// “Unreachable” does not mean “absent from the bundle”. Two measured incidents:
//   1. At 13d400a this comment claimed the entire mock module would be removed.
//      Bundle inspection found tenants, keys, and templates removed, but WHOAMI
//      remained because `let identity = WHOAMI.admin` is a top-level property
//      access that might invoke a getter. The minifier kept the object, and only
//      a fragment such as `({admin:{...key_id:"0f1e…"}})` remained.
//   2. Commit 73fe5e0 removed the top-level reference (null initial value plus
//      currentIdentity()). A later bundle scan found zero fake identities,
//      tenants, or key IDs. The current build does exclude mock data.
// This is not permission to add arbitrary mock content. The property depends on
// mock.ts keeping only pure literals at its top level; any property access,
// function call, or spread can preserve data again. Never add real or realistic
// credentials there. After changing mock top-level code, rescan bundles for fake
// identity values too—searching only `sk_` or `Bearer` missed the first incident.
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "1";

export const api: ControlPlaneApi = USE_MOCK ? mockApi : liveApi;
export { USE_MOCK };
