import {
  Ban,
  KeyRound,
  LoaderCircle,
  Plus,
  Undo2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { TenantStatus } from "../api";
import { formatDbRelative, formatDbTime } from "../format";
import { errorText, useI18n, type LocalizedError } from "../i18n";
import type { ApiKeyView, IssuedKeyView, TenantView } from "../types";
import { RefreshButton } from "./RefreshButton";
import SecretDialog from "./SecretDialog";

/**
 * Tenant administration.
 *
 * Creates tenants, changes status, and issues or revokes tenant keys. Workspace
 * reclamation remains on the sandbox page.
 *
 * Every button changes cluster-wide authorization. This page deliberately does not
 * auto-poll: a table reorder at the moment a user clicks “revoke” could target the
 * wrong key. Refreshes occur only after explicit user action or successful writes.
 * Plaintext keys exist only in SecretDialog props and are never persisted.
 */

/** Must match `TENANT_ID` in store.py because the ID is also a Kubernetes label. */
const TENANT_ID_PATTERN = /^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$/;

const DEFAULT_MAX_WORKSPACES = 3;
const DEFAULT_MAX_RUNTIMES = 2;

export default function TenantsView({
  onError,
}: {
  onError: (cause: unknown) => void;
}) {
  const { locale, t, tPlural } = useI18n();
  const [tenants, setTenants] = useState<TenantView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<LocalizedError | null>(null);
  const [draftId, setDraftId] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftWorkspaces, setDraftWorkspaces] = useState(
    String(DEFAULT_MAX_WORKSPACES),
  );
  const [draftRuntimes, setDraftRuntimes] = useState(String(DEFAULT_MAX_RUNTIMES));
  // Load key lists only for the expanded tenant. Fetching all keys eagerly would
  // issue one request per tenant even though most rows are never expanded.
  const [inspecting, setInspecting] = useState("");
  const [keys, setKeys] = useState<ApiKeyView[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [keyLabel, setKeyLabel] = useState("");
  // Plaintext keys live only here and disappear when the dialog closes.
  const [issued, setIssued] = useState<IssuedKeyView | null>(null);
  const keysPanel = useRef<HTMLElement>(null);

  // The key panel renders below the whole tenant table. Opened from a row near
  // the bottom it would land off-screen and "View keys" would look like a no-op.
  useEffect(() => {
    if (inspecting) {
      keysPanel.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [inspecting]);

  const load = useCallback(async () => {
    try {
      setTenants(await api.listTenants());
    } catch (cause) {
      onError(cause);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadKeys = useCallback(
    async (tenantId: string) => {
      setKeysLoading(true);
      try {
        setKeys(await api.listApiKeys(tenantId));
      } catch (cause) {
        setKeys([]);
        onError(cause);
      } finally {
        setKeysLoading(false);
      }
    },
    [onError],
  );

  const inspect = (tenantId: string) => {
    if (inspecting === tenantId) {
      setInspecting("");
      setKeys([]);
      return;
    }
    setInspecting(tenantId);
    setKeyLabel("");
    void loadKeys(tenantId);
  };

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const id = draftId.trim();
    const maxWorkspaces = Number(draftWorkspaces);
    const maxRuntimes = Number(draftRuntimes);
    if (!TENANT_ID_PATTERN.test(id)) {
      setFormError({ key: "tenants.invalidId" });
      return;
    }
    // Store rejects quotas below one. Explain locally to avoid a guaranteed failed
    // network round trip.
    if (
      !Number.isInteger(maxWorkspaces)
      || maxWorkspaces < 1
      || !Number.isInteger(maxRuntimes)
      || maxRuntimes < 1
    ) {
      setFormError({ key: "tenants.invalidQuota" });
      return;
    }
    setFormError(null);
    setBusy("__create__");
    try {
      await api.createTenant({
        id,
        display_name: draftName.trim() || id,
        max_workspaces: maxWorkspaces,
        max_runtimes: maxRuntimes,
      });
      setCreating(false);
      setDraftId("");
      setDraftName("");
      await load();
    } catch (cause) {
      // Keep create errors near the form. A global banner would separate the message
      // from the input that caused it.
      setFormError({
        message: cause instanceof Error ? cause.message : String(cause),
      });
    } finally {
      setBusy("");
    }
  };

  /**
   * Suspend and restore share POST /status.
   *
   * Only suspension asks for confirmation because it immediately rejects every caller
   * for the tenant. Restoring is reversible and does not warrant habituating users to
   * confirmation dialogs.
   */
  const setStatus = async (tenant: TenantView, status: TenantStatus) => {
    if (
      status === "suspended"
      && !window.confirm(t("tenants.confirmSuspend", { id: tenant.id }))
    ) {
      return;
    }
    setBusy(tenant.id);
    try {
      await api.setTenantStatus(tenant.id, status);
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const issueKey = async (tenantId: string) => {
    if (busy === `${tenantId}:key`) {
      return;
    }
    setBusy(`${tenantId}:key`);
    try {
      setIssued(await api.issueApiKey(tenantId, keyLabel.trim() || "unnamed"));
      setKeyLabel("");
      await loadKeys(tenantId);
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const revokeKey = async (key: ApiKeyView) => {
    if (!window.confirm(t("tenants.confirmRevoke", {
      label: key.label,
      prefix: key.key_prefix,
    }))) {
      return;
    }
    setBusy(key.id);
    try {
      await api.revokeApiKey(key.id);
      if (inspecting) {
        await loadKeys(inspecting);
      }
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page">
      <section className="card">
        <div className="card-header">
          <h2 className="card-title">{t("tenants.title")}</h2>
          <div className="header-actions">
            <span className="card-count">
              {tPlural("tenants.count", tenants.length)}
            </span>
            <RefreshButton onRefresh={load}>
              {t("common.refresh")}
            </RefreshButton>
            <button
              type="button"
              className="button button-small button-primary"
              onClick={() => {
                setCreating((value) => !value);
                setFormError(null);
              }}
            >
              <Plus size={15} aria-hidden="true" />
              {t("tenants.new")}
            </button>
          </div>
        </div>

        {creating ? (
          <form className="form-panel" onSubmit={(event) => void submitCreate(event)}>
            <label className="field">
              <span>{t("tenants.tenantId")}</span>
              <input
                value={draftId}
                placeholder="acme"
                autoFocus
                autoCapitalize="none"
                autoCorrect="off"
                maxLength={32}
                spellCheck={false}
                onChange={(event) => setDraftId(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("tenants.displayName")}</span>
              <input
                value={draftName}
                placeholder={t("tenants.displayNamePlaceholder")}
                onChange={(event) => setDraftName(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("tenants.workspaceLimit")}</span>
              <input
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={draftWorkspaces}
                onChange={(event) => setDraftWorkspaces(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("tenants.runtimeLimit")}</span>
              <input
                type="number"
                min={1}
                step={1}
                inputMode="numeric"
                value={draftRuntimes}
                onChange={(event) => setDraftRuntimes(event.target.value)}
              />
            </label>
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={busy === "__create__"}
              >
                {busy === "__create__" ? (
                  <LoaderCircle className="spin" size={16} />
                ) : null}
                {t("common.create")}
              </button>
              <button
                type="button"
                className="button"
                onClick={() => setCreating(false)}
              >
                {t("common.cancel")}
              </button>
            </div>
            <p className="form-note">{t("tenants.createNote")}</p>
            {formError ? (
              <p className="form-error" role="alert">
                {errorText(formError, t)}
              </p>
            ) : null}
          </form>
        ) : null}

        {loading ? (
          <div className="state">
            <LoaderCircle className="spin" size={20} />
            <strong>{t("common.loadingShort")}</strong>
          </div>
        ) : tenants.length === 0 ? (
          <p className="empty">{t("tenants.empty")}</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t("tenants.tenantId")}</th>
                  <th>{t("tenants.displayName")}</th>
                  <th>{t("tenants.workspace")}</th>
                  <th>{t("tenants.runtimeLimit")}</th>
                  <th>{t("common.status")}</th>
                  <th aria-label={t("common.actions")} />
                </tr>
              </thead>
              <tbody>
                {tenants.map((tenant) => {
                  const suspended = tenant.status !== "active";
                  const rowBusy = busy === tenant.id;
                  // Existing workspaces are not deleted retroactively when a quota is
                  // lowered. Mark over-quota usage rather than silently showing 4/1.
                  const overQuota = tenant.workspaces_in_use > tenant.max_workspaces;
                  return (
                    <tr
                      key={tenant.id}
                      className={suspended ? "row-disabled" : undefined}
                    >
                      <td className="mono">{tenant.id}</td>
                      <td className="cell-wide">{tenant.display_name}</td>
                      <td className="mono">
                        <span className={overQuota ? "over-quota" : undefined}>
                          {tenant.workspaces_in_use} / {tenant.max_workspaces}
                        </span>
                      </td>
                      <td className="mono">{tenant.max_runtimes}</td>
                      <td>
                        <span className={`badge ${suspended ? "badge-bad" : "badge-ok"}`}>
                          {suspended ? t("common.suspended") : t("common.active")}
                        </span>
                      </td>
                      <td className="cell-actions">
                        <button
                          type="button"
                          className="button button-small"
                          aria-expanded={inspecting === tenant.id}
                          onClick={() => inspect(tenant.id)}
                        >
                          <KeyRound size={14} aria-hidden="true" />
                          {inspecting === tenant.id
                            ? t("tenants.hideKeys")
                            : t("tenants.showKeys")}
                        </button>
                        {suspended ? (
                          <button
                            type="button"
                            className="button button-small"
                            disabled={rowBusy}
                            onClick={() => void setStatus(tenant, "active")}
                          >
                            {rowBusy ? (
                              <LoaderCircle className="spin" size={14} />
                            ) : (
                              <Undo2 size={14} aria-hidden="true" />
                            )}
                            {t("tenants.restore")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="button button-small button-danger"
                            disabled={rowBusy}
                            onClick={() => void setStatus(tenant, "suspended")}
                          >
                            {rowBusy ? (
                              <LoaderCircle className="spin" size={14} />
                            ) : (
                              <Ban size={14} aria-hidden="true" />
                            )}
                            {t("tenants.suspend")}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {inspecting ? (
        <section className="card" ref={keysPanel}>
          <div className="card-header">
            <h2 className="card-title">
              <KeyRound size={16} aria-hidden="true" />
              <span className="mono">
                {t("tenants.apiKeysFor", { id: inspecting })}
              </span>
            </h2>
            <span className="card-count">
              {tPlural("tenants.keyCount", keys.length)}
            </span>
          </div>

          <form
            className="form-panel"
            onSubmit={(event) => {
              event.preventDefault();
              void issueKey(inspecting);
            }}
          >
            <label className="field">
              <span>{t("tenants.keyLabel")}</span>
              <input
                value={keyLabel}
                placeholder={t("tenants.keyLabelPlaceholder")}
                onChange={(event) => setKeyLabel(event.target.value)}
              />
            </label>
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={busy === `${inspecting}:key`}
              >
                {busy === `${inspecting}:key` ? (
                  <LoaderCircle className="spin" size={16} />
                ) : null}
                {t("tenants.issueKey")}
              </button>
            </div>
            <p className="form-note">{t("tenants.keyLabelNote")}</p>
          </form>

          {keysLoading ? (
            <div className="state">
              <LoaderCircle className="spin" size={20} />
              <strong>{t("common.loadingShort")}</strong>
            </div>
          ) : keys.length === 0 ? (
            <p className="empty">{t("tenants.keysEmpty")}</p>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>{t("tenants.keyLabel")}</th>
                    <th>{t("tenants.prefix")}</th>
                    <th>{t("common.created")}</th>
                    <th title={t("tenants.lastUsedThrottle")}>
                      {t("tenants.lastUsed")}
                    </th>
                    <th>{t("common.status")}</th>
                    <th aria-label={t("common.actions")} />
                  </tr>
                </thead>
                <tbody>
                  {keys.map((key) => {
                    const revoked = Boolean(key.revoked_at);
                    return (
                      <tr
                        key={key.id}
                        className={revoked ? "row-disabled" : undefined}
                      >
                        <td className="cell-wide">{key.label}</td>
                        <td className="mono">{key.key_prefix}…</td>
                        <td className="mono">{formatDbTime(key.created_at, locale)}</td>
                        {/* Null means never used, which is materially different from
                            “last used a long time ago” for revocation decisions. */}
                        <td className="mono">
                          {key.last_used_at ? (
                            formatDbRelative(key.last_used_at, locale)
                          ) : (
                            <span className="subtle">{t("tenants.neverUsed")}</span>
                          )}
                        </td>
                        <td>
                          <span className={`badge ${revoked ? "badge-muted" : "badge-ok"}`}>
                            {revoked
                              ? t("tenants.revokedAt", {
                                time: formatDbTime(key.revoked_at, locale),
                              })
                              : t("tenants.valid")}
                          </span>
                        </td>
                        <td className="cell-actions">
                          <button
                            type="button"
                            className="button button-small button-danger"
                            disabled={revoked || busy === key.id}
                            onClick={() => void revokeKey(key)}
                          >
                            {busy === key.id ? (
                              <LoaderCircle className="spin" size={14} />
                            ) : (
                              <Ban size={14} aria-hidden="true" />
                            )}
                            {t("tenants.revoke")}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ) : null}

      {issued ? (
        <SecretDialog
          title={t("tenants.apiKeyTitle")}
          subject={`${issued.tenant_id} / ${issued.label}`}
          secret={issued.api_key}
          note={issued.note}
          onClose={() => setIssued(null)}
        />
      ) : null}
    </div>
  );
}
