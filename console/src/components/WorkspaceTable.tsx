import {
  CircleAlert,
  CircleCheck,
  FolderOpen,
  HardDrive,
  Link2,
  Timer,
  Trash2,
} from "lucide-react";
import { formatRelative, formatUnix, isPast } from "../format";
import { useI18n } from "../i18n";
import type { WorkspaceView } from "../types";

/**
 * Workspace list.
 *
 * The reclaim column makes both `runtime_attached` and `idle_expires_at` visible.
 * Control Plane reclaims only workspaces with no active runtime whose idle deadline has
 * passed. A countdown alone would make an attached workspace look overdue when it
 * is correctly retained.
 */

export default function WorkspaceTable({
  workspaces,
  onDelete,
  onBrowse,
  pendingId,
  browsingId,
}: {
  workspaces: WorkspaceView[];
  onDelete: (id: string) => void;
  onBrowse: (id: string) => void;
  pendingId: string | null;
  browsingId: string | null;
}) {
  const { locale, t, tPlural } = useI18n();

  return (
    <section className="card" aria-labelledby="workspaces-title">
      <div className="card-header">
        <h2 className="card-title" id="workspaces-title">
          <HardDrive size={18} aria-hidden="true" />
          {t("workspace.title")}
        </h2>
        <span className="card-count">
          {tPlural("workspace.count", workspaces.length)}
        </span>
      </div>
      {workspaces.length === 0 ? (
        <p className="empty">{t("workspace.empty")}</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">ID</th>
                <th scope="col">{t("common.status")}</th>
                <th scope="col">{t("common.created")}</th>
                <th scope="col">{t("workspace.lastUsed")}</th>
                <th scope="col">{t("workspace.reclaim")}</th>
                <th scope="col">
                  <span className="visually-hidden">{t("common.actions")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {workspaces.map((item, index) => {
                const id = item.id ?? "";
                const overdue = isPast(item.idle_expires_at);
                return (
                  <tr key={id || `row-${index}`}>
                    <td className="mono">{id || "—"}</td>
                    <td>
                      {item.status === "ready" ? (
                        <span className="badge badge-ok">
                          <CircleCheck size={14} aria-hidden="true" />
                          ready
                        </span>
                      ) : (
                        <span className="badge badge-warn">
                          <CircleAlert size={14} aria-hidden="true" />
                          {item.status}
                        </span>
                      )}
                    </td>
                    <td className="mono" title={formatUnix(item.created_at, locale)}>
                      {formatRelative(item.created_at, undefined, locale)}
                    </td>
                    <td className="mono" title={formatUnix(item.last_used_at, locale)}>
                      {formatRelative(item.last_used_at, undefined, locale)}
                    </td>
                    <td>
                      {item.runtime_attached ? (
                        <span className="badge badge-muted">
                          <Link2 size={14} aria-hidden="true" />
                          {t("workspace.runtimeAttached")}
                        </span>
                      ) : (
                        <span
                          className={overdue ? "badge badge-bad" : "badge badge-warn"}
                          title={formatUnix(item.idle_expires_at, locale)}
                        >
                          <Timer size={14} aria-hidden="true" />
                          {overdue
                            ? t("workspace.pendingReclaim")
                            : formatRelative(item.idle_expires_at, undefined, locale)}
                        </span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="button button-small"
                        onClick={() => onBrowse(id)}
                        disabled={!id || browsingId === id}
                        aria-label={t("workspace.browseFiles", { id })}
                      >
                        <FolderOpen size={14} aria-hidden="true" />
                        {t("workspace.files")}
                      </button>{" "}
                      <button
                        type="button"
                        className="button button-small button-danger"
                        onClick={() => onDelete(id)}
                        disabled={!id || pendingId === id}
                        aria-label={t("workspace.deleteWorkspace", { id })}
                      >
                        <Trash2 size={14} aria-hidden="true" />
                        {pendingId === id ? t("workspace.deleting") : t("workspace.delete")}
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
  );
}
