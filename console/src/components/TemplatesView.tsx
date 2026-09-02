import { Info, LoaderCircle, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ControlPlaneError } from "../api";
import { formatDbTime } from "../format";
import { errorText, useI18n, type LocalizedError } from "../i18n";
import type { TemplateRecordView } from "../types";
import { RefreshButton } from "./RefreshButton";

/**
 * Sandbox template management.
 *
 * A template is an image and therefore permission to run code in the cluster. The
 * write form is rendered only when capabilities contain `templates:write`, which
 * Control Plane grants only when an image allowlist is configured. Do not infer access from
 * identity kind.
 *
 * Two read endpoints intentionally have different shapes:
 * - `/v1/templates` returns every selectable ID, including built-ins.
 * - `/v1/admin/templates` returns complete records, including image and creator.
 */

/** Tenant ID used for global templates ('*' rather than NULL in PostgreSQL keys). */
const GLOBAL_TENANT = "*";

export default function TemplatesView({
  canReadAll,
  canWrite,
  onError,
}: {
  /**
   * The capability grants `/v1/admin/templates`. It is not equivalent to
   * `kind === "admin"`: an admin key forwarded with X-Sandbox-Tenant has admin kind
   * but not this capability.
   */
  canReadAll: boolean;
  canWrite: boolean;
  onError: (cause: unknown) => void;
}) {
  const { locale, t, tPlural } = useI18n();
  const [ids, setIds] = useState<string[]>([]);
  const [records, setRecords] = useState<TemplateRecordView[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<LocalizedError | null>(null);
  const [draftId, setDraftId] = useState("");
  const [draftImage, setDraftImage] = useState("");
  const [draftTenant, setDraftTenant] = useState(GLOBAL_TENANT);

  const load = useCallback(async () => {
    try {
      setIds(await api.listTemplateIds());
      if (!canReadAll) {
        setRecords(null);
        return;
      }
      try {
        setRecords(await api.listTemplateRecords());
      } catch (cause) {
        // A 403 means local capabilities and Control Plane's current version disagree.
        // Fall back to the ID-only list rather than making the whole page unusable.
        if (cause instanceof ControlPlaneError && cause.status === 403) {
          setRecords(null);
          return;
        }
        throw cause;
      }
    } catch (cause) {
      onError(cause);
    } finally {
      setLoading(false);
    }
  }, [canReadAll, onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const templateId = draftId.trim();
    const image = draftImage.trim();
    if (!templateId || !image) {
      setFormError({ key: "templates.invalidInput" });
      return;
    }
    setFormError(null);
    setBusy("__create__");
    try {
      await api.createTemplate({
        template_id: templateId,
        image,
        tenant_id: draftTenant.trim() || GLOBAL_TENANT,
      });
      setCreating(false);
      setDraftId("");
      setDraftImage("");
      await load();
    } catch (cause) {
      // Control Plane validates the image allowlist before the ID. Preserve the server error
      // rather than duplicating rules that could drift.
      setFormError({
        message: cause instanceof Error ? cause.message : String(cause),
      });
    } finally {
      setBusy("");
    }
  };

  const remove = async (record: TemplateRecordView) => {
    const scope = record.tenant_id === GLOBAL_TENANT
      ? t("templates.global")
      : t("templates.tenantScope", { id: record.tenant_id });
    if (!window.confirm(t("templates.confirmDelete", {
      scope,
      id: record.template_id,
    }))) {
      return;
    }
    setBusy(`${record.tenant_id}/${record.template_id}`);
    try {
      await api.deleteTemplate(record.template_id, record.tenant_id);
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const recordedIds = new Set(
    (records ?? []).map((record) => record.template_id),
  );

  return (
    <div className="page">
      <section className="card">
        <div className="card-header">
          <h2 className="card-title">{t("templates.title")}</h2>
          <div className="header-actions">
            <span className="card-count">
              {tPlural("templates.count", ids.length)}
            </span>
            <RefreshButton onRefresh={load}>
              {t("common.refresh")}
            </RefreshButton>
            {canWrite ? (
              <button
                type="button"
                className="button button-small button-primary"
                onClick={() => {
                  setCreating((value) => !value);
                  setFormError(null);
                }}
              >
                <Plus size={15} aria-hidden="true" />
                {t("templates.new")}
              </button>
            ) : null}
          </div>
        </div>

        {canReadAll && !canWrite ? (
          <div className="banner banner-warn" role="status">
            <Info size={18} aria-hidden="true" />
            <div>
              <strong>{t("templates.writeUnavailable")}</strong>
              <p>{t("templates.writeUnavailableBody")}</p>
            </div>
          </div>
        ) : null}

        {creating ? (
          <form className="form-panel" onSubmit={(event) => void submitCreate(event)}>
            <label className="field">
              <span>{t("templates.templateId")}</span>
              <input
                value={draftId}
                placeholder="playwright"
                spellCheck={false}
                onChange={(event) => setDraftId(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("templates.image")}</span>
              <input
                value={draftImage}
                placeholder="ghcr.io/convee/sandbox-xxx:tag"
                spellCheck={false}
                onChange={(event) => setDraftImage(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("templates.visibility")}</span>
              <input
                value={draftTenant}
                placeholder={GLOBAL_TENANT}
                spellCheck={false}
                onChange={(event) => setDraftTenant(event.target.value)}
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
                {t("templates.add")}
              </button>
              <button
                type="button"
                className="button"
                onClick={() => setCreating(false)}
              >
                {t("common.cancel")}
              </button>
            </div>
            <p className="form-note">
              {t("templates.visibilityNote", { global: GLOBAL_TENANT })}
            </p>
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
        ) : ids.length === 0 ? (
          <p className="empty">{t("templates.empty")}</p>
        ) : (
          <ul className="chip-list">
            {ids.map((id) => (
              <li key={id} className="chip">
                <span className="mono">{id}</span>
                {/* Built-ins are read-only and cannot be overridden. Marking them
                    prevents an administrator from creating a duplicate by looking for
                    them in the database table below. */}
                {records && !recordedIds.has(id) ? (
                  <span className="badge badge-muted">{t("templates.builtin")}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      {records ? (
        <section className="card">
          <div className="card-header">
            <h2 className="card-title">{t("templates.databaseTitle")}</h2>
            <span className="card-count">
              {tPlural("common.records", records.length)}
            </span>
          </div>
          {records.length === 0 ? (
            <p className="empty">{t("templates.databaseEmpty")}</p>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>{t("templates.templateId")}</th>
                    <th>{t("templates.imageTitle")}</th>
                    <th>{t("templates.visibility")}</th>
                    <th>{t("common.created")}</th>
                    <th>{t("templates.createdBy")}</th>
                    {canWrite ? <th aria-label={t("common.actions")} /> : null}
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => {
                    const rowKey = `${record.tenant_id}/${record.template_id}`;
                    return (
                      <tr key={rowKey}>
                        <td className="mono">{record.template_id}</td>
                        <td className="mono cell-wide">
                          {record.image}
                          {/* A tighter allowlist stops this record from taking effect
                              immediately. The badge explains why an otherwise normal row
                              is no longer selectable. */}
                          {record.allowed ? null : (
                            <>
                              {" "}
                              <span
                                className="badge badge-bad"
                                title={t("templates.outsideAllowlistTitle")}
                              >
                                {t("templates.outsideAllowlist")}
                              </span>
                            </>
                          )}
                        </td>
                        <td>
                          {record.tenant_id === GLOBAL_TENANT ? (
                            <span className="badge badge-muted">
                              {t("templates.global")}
                            </span>
                          ) : (
                            <span className="mono">{record.tenant_id}</span>
                          )}
                        </td>
                        <td className="mono">
                          {formatDbTime(record.created_at, locale)}
                        </td>
                        <td className="mono">{record.created_by ?? "—"}</td>
                        {canWrite ? (
                          <td className="cell-actions">
                            <button
                              type="button"
                              className="button button-small button-danger"
                              disabled={busy === rowKey}
                              onClick={() => void remove(record)}
                            >
                              {busy === rowKey ? (
                                <LoaderCircle className="spin" size={14} />
                              ) : (
                                <Trash2 size={14} aria-hidden="true" />
                              )}
                              {t("common.delete")}
                            </button>
                          </td>
                        ) : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}
