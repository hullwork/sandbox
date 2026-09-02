import {
  Activity,
  Boxes,
  Layers,
  LogOut,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ControlPlaneError, USE_MOCK } from "./api";
import { clearToken, loadToken, saveToken } from "./auth";
import LoginView from "./components/LoginView";
import ObservabilityView from "./components/ObservabilityView";
import OverviewView from "./components/OverviewView";
import TemplatesView from "./components/TemplatesView";
import TenantsView from "./components/TenantsView";
import {
  errorText,
  LanguageSwitcher,
  useI18n,
  type LocalizedError,
  type Translator,
} from "./i18n";
import type { TranslationKey } from "./i18n/locales/en";
import type { WhoamiView } from "./types";

/**
 * Application shell for Sandbox Console.
 *
 * This component owns authentication state, capability-driven navigation, and the
 * global error banner. Each page loads its own data.
 *
 * Security contract: `/v1/whoami` capabilities determine which tabs are rendered;
 * do not infer access from `kind`. When no image allowlist is configured, even an
 * admin does not receive `templates:write`, and rendering a write form would create
 * a guaranteed 409. Tab visibility is presentation only; Control Plane remains the
 * authorization authority.
 *
 * The console intentionally has no URL routing. Once query parameters carry UI
 * state, credentials eventually follow.
 */

type Tab = "overview" | "tenants" | "templates" | "observability";

const TABS: readonly Tab[] = ["overview", "tenants", "templates", "observability"];
// Which section is open survives a reload, in the same tab-scoped storage as the
// credential and cleared with it. It is UI state, not a credential, and it never
// reaches the URL. A tab the identity cannot see falls back to the overview.
const TAB_KEY = "sandbox-console-tab";

function loadTab(): Tab {
  try {
    const saved = window.sessionStorage.getItem(TAB_KEY);
    return TABS.includes(saved as Tab) ? (saved as Tab) : "overview";
  } catch {
    return "overview";
  }
}

function saveTab(tab: Tab): void {
  try {
    window.sessionStorage.setItem(TAB_KEY, tab);
  } catch {
    // Storage can be unavailable; the selection still applies in memory.
  }
}

export default function App() {
  const { t } = useI18n();
  const [token, setToken] = useState(loadToken);
  // A Console session lives in an HttpOnly cookie minted by the OIDC callback;
  // the browser never sees a credential for it, so there is nothing to store.
  const [ssoSession, setSsoSession] = useState(false);
  const [checkingSession, setCheckingSession] = useState(!loadToken());
  const [whoami, setWhoami] = useState<WhoamiView | null>(null);
  const [tab, setTab] = useState<Tab>(loadTab);
  const [error, setError] = useState<LocalizedError | null>(null);
  const [sessionNote, setSessionNote] = useState<TranslationKey | null>(null);

  const logout = useCallback((note: TranslationKey) => {
    // Only a single-sign-on session has anything server-side to end. An API-key
    // session would just produce a pointless 401 in the Control Plane log.
    if (ssoSession) {
      void api.logout().catch(() => undefined);
    }
    clearToken();
    setToken("");
    setSsoSession(false);
    setWhoami(null);
    setError(null);
    setTab("overview");
    setSessionNote(note);
  }, [ssoSession]);

  const selectTab = useCallback((next: Tab) => {
    setTab(next);
    saveTab(next);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  useEffect(() => {
    if (token) return;
    let cancelled = false;
    void api.whoami("").then((identity) => {
      if (!cancelled) {
        setSsoSession(true);
        setWhoami(identity);
      }
    }).catch(() => undefined).finally(() => {
      if (!cancelled) setCheckingSession(false);
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  /**
   * Shared page error handler. A 401 invalidates the credential; staying in the
   * console would make every polling page repeat the same failure. Return to login.
   */
  const handleError = useCallback((cause: unknown) => {
    if (cause instanceof ControlPlaneError && cause.status === 401) {
      logout("app.sessionExpired");
      return;
    }
    setError(
      cause instanceof ControlPlaneError && cause.status === 0
        ? { key: "api.networkError", values: { message: cause.message } }
        : cause instanceof Error
            ? { message: cause.message }
            : { message: String(cause) },
    );
  }, [logout]);

  // sessionStorage may still hold a credential after a refresh, while identity is
  // no longer in memory. Ask Control Plane again instead of persisting capabilities: cached
  // capabilities could misleadingly survive a server-side revocation.
  useEffect(() => {
    if (!token || whoami) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const next = await api.whoami();
        if (!cancelled) {
          setWhoami(next);
        }
      } catch (cause) {
        if (!cancelled) {
          handleError(cause);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [handleError, token, whoami]);

  // Clear page-specific errors on navigation. A stale banner from another endpoint
  // would incorrectly suggest that the new page is also failing.
  useEffect(() => {
    setError(null);
  }, [tab]);

  if (checkingSession) {
    return <p className="session-note" role="status">{t("app.checkingSession")}</p>;
  }

  if (!token && !ssoSession) {
    return (
      <>
        {sessionNote ? (
          <p className="session-note" role="status">{t(sessionNote)}</p>
        ) : null}
        <LoginView
          onAuthenticated={(next, identity) => {
            saveToken(next);
            setToken(next);
            setSsoSession(false);
            setWhoami(identity);
            setSessionNote(null);
          }}
        />
      </>
    );
  }

  const capabilities = whoami?.capabilities ?? [];
  const canManageTenants = capabilities.includes("tenants:write");
  const canWriteTemplates = capabilities.includes("templates:write");
  // Use capabilities rather than kind for full-template access. The break-glass
  // token has admin-equivalent capabilities, while an admin key forwarded with
  // X-Sandbox-Tenant can have kind=admin without `templates:read:all`.
  const canReadAllTemplates = capabilities.includes("templates:read:all");
  // Control Plane returns this block only to an administrator and only when a Grafana
  // is configured, so its presence is the whole condition. Absent means either
  // "no Grafana in this deployment" or "you are a tenant"; both render no tab,
  // and Control Plane answers 404/403 on `/grafana/*` regardless of what is rendered.
  const grafana = whoami?.grafana?.enabled ? whoami.grafana : null;

  const tabs: Array<{ id: Tab; label: string; icon: ReactNode }> = [
    { id: "overview" as const, label: t("app.tab.overview"), icon: <Boxes size={16} /> },
    ...(canManageTenants
      ? [{ id: "tenants" as const, label: t("app.tab.tenants"), icon: <Users size={16} /> }]
      : []),
    { id: "templates", label: t("app.tab.templates"), icon: <Layers size={16} /> },
    ...(grafana
      ? [{
          id: "observability" as const,
          label: t("app.tab.observability"),
          icon: <Activity size={16} />,
        }]
      : []),
  ];
  // The remembered tab may belong to a capability this identity lacks, or to one
  // whoami has not confirmed yet. Render what is visible, keep the preference.
  const activeTab: Tab = tabs.some((item) => item.id === tab) ? tab : "overview";

  return (
    <div className="app">
      <aside className="app-sidebar">
        <header className="app-header">
          <div className="app-heading">
            <h1 className="app-title">Sandbox Console</h1>
            <p className="app-subtitle">
              {whoami ? describeIdentity(whoami, t) : t("app.identity.pending")}
              {USE_MOCK ? t("app.mockMode") : ""}
            </p>
          </div>
        </header>

        <nav className="tabs" aria-label={t("app.tabs")}>
          {tabs.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tab ${activeTab === item.id ? "is-active" : ""}`}
              aria-current={activeTab === item.id ? "page" : undefined}
              onClick={() => selectTab(item.id)}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        <LanguageSwitcher className="sidebar-language" />
        <button
          type="button"
          className="button button-small sidebar-logout"
          aria-label={t("app.signOut")}
          onClick={() => logout("app.loggedOut")}
        >
          <LogOut size={15} aria-hidden="true" />
          <span>{t("app.signOut")}</span>
        </button>
      </aside>

      <main className="app-main">
        {/*
          The static SANDBOX_CONTROL_PLANE_TOKEN is an escape hatch, not a way to work. Every
          use of it is written to the Control Plane log; this banner is the matching
          signal on the screen. Do not collapse or remove it because it looks
          noisy - noticing that someone is holding the escape hatch open is the
          entire point.
        */}
        {whoami?.kind === "break-glass" ? (
          <div className="banner banner-warn" role="status">
            <TriangleAlert size={18} aria-hidden="true" />
            <div>
              <strong>{t("app.breakGlass.title")}</strong>
              <p>{t("app.breakGlass.body")}</p>
            </div>
          </div>
        ) : null}

        {error ? (
          <div className="banner banner-bad" role="alert">
            <TriangleAlert size={18} aria-hidden="true" />
            <div><p>{errorText(error, t)}</p></div>
            <button
              type="button"
              className="icon-button"
              aria-label={t("app.closeNotice")}
              onClick={() => setError(null)}
            >
              <X size={16} />
            </button>
          </div>
        ) : null}

        {activeTab === "overview" ? (
          <OverviewView onError={handleError} onRecover={clearError} />
        ) : null}
        {activeTab === "tenants" && canManageTenants ? (
          <TenantsView onError={handleError} />
        ) : null}
        {activeTab === "templates" ? (
          <TemplatesView
            canReadAll={canReadAllTemplates}
            canWrite={canWriteTemplates}
            onError={handleError}
          />
        ) : null}
        {activeTab === "observability" && grafana ? (
          <ObservabilityView grafana={grafana} />
        ) : null}
      </main>
    </div>
  );
}

/**
 * Builds the sidebar identity line. Tenant identities include quota because that
 * answers the most common “why can I not create another workspace?” question.
 */
function describeIdentity(whoami: WhoamiView, t: Translator): string {
  if (whoami.kind === "break-glass") {
    return t("app.identity.breakGlass");
  }
  if (whoami.kind === "admin") {
    return t("app.identity.admin", {
      keyId: whoami.key_id ? t("app.identity.adminKeyId", { keyId: whoami.key_id }) : "",
    });
  }
  const tenant = whoami.tenant;
  const scope = whoami.tenant_id ?? t("app.identity.unknownTenant");
  if (!tenant) {
    return t("app.identity.tenant", { scope });
  }
  return t("app.identity.tenantDetail", {
    scope,
    name: tenant.name,
    used: tenant.used_workspaces,
    max: tenant.max_workspaces,
    suspended: tenant.status === "active"
      ? ""
      : t("app.identity.suspendedSuffix"),
  });
}
