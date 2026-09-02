import { Box, CircleAlert, CircleCheck, ShieldCheck, ShieldOff, Trash2 } from "lucide-react";
import { formatRelative, formatUnix, isPast } from "../format";
import { useI18n } from "../i18n";
import type { SandboxView } from "../types";

/**
 * Runtime list.
 *
 * Renders Control Plane's sandbox view and supports reclaiming one runtime at a time.
 * It never creates sandboxes; backend sessions own creation, and a console-created
 * sandbox would have no owner.
 *
 * An empty `runtime_class` means gVisor kernel isolation was unavailable. That is a
 * security downgrade and must be explicit rather than rendered as blank.
 */

export default function SandboxTable({
  sandboxes,
  onDelete,
  pendingId,
}: {
  sandboxes: SandboxView[];
  onDelete: (id: string) => void;
  pendingId: string | null;
}) {
  const { locale, t, tPlural } = useI18n();

  return (
    <section className="card" aria-labelledby="sandboxes-title">
      <div className="card-header">
        <h2 className="card-title" id="sandboxes-title">
          <Box size={18} aria-hidden="true" />
          {t("sandbox.title")}
        </h2>
        <span className="card-count">
          {tPlural("sandbox.count", sandboxes.length)}
        </span>
      </div>
      {sandboxes.length === 0 ? (
        <p className="empty">{t("sandbox.empty")}</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">ID</th>
                <th scope="col">{t("common.status")}</th>
                <th scope="col">{t("sandbox.isolation")}</th>
                <th scope="col">{t("sandbox.template")}</th>
                <th scope="col">{t("sandbox.workspace")}</th>
                <th scope="col">{t("common.created")}</th>
                <th scope="col">{t("sandbox.expires")}</th>
                <th scope="col">
                  <span className="visually-hidden">{t("common.actions")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {sandboxes.map((item) => {
                const id = item.id ?? "";
                const expired = isPast(item.expires_at);
                return (
                  <tr key={id || Math.random()}>
                    <td className="mono">{id || "—"}</td>
                    <td>
                      {item.status === "running" ? (
                        <span className="badge badge-ok">
                          <CircleCheck size={14} aria-hidden="true" />
                          running
                        </span>
                      ) : (
                        <span className="badge badge-warn">
                          <CircleAlert size={14} aria-hidden="true" />
                          {item.status}
                        </span>
                      )}
                    </td>
                    <td>
                      {item.runtime_class ? (
                        <span className="badge badge-ok">
                          <ShieldCheck size={14} aria-hidden="true" />
                          {item.runtime_class}
                        </span>
                      ) : (
                        <span className="badge badge-bad">
                          <ShieldOff size={14} aria-hidden="true" />
                          {t("sandbox.noKernelIsolation")}
                        </span>
                      )}
                    </td>
                    <td className="mono">{item.template}</td>
                    <td className="mono">{item.workspace_id ?? "—"}</td>
                    <td className="mono" title={formatUnix(item.created_at, locale)}>
                      {formatRelative(item.created_at, undefined, locale)}
                    </td>
                    <td
                      className="mono"
                      title={formatUnix(item.expires_at, locale)}
                      style={expired ? { color: "var(--danger)" } : undefined}
                    >
                      {formatRelative(item.expires_at, undefined, locale)}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="button button-small button-danger"
                        onClick={() => onDelete(id)}
                        disabled={!id || pendingId === id}
                        aria-label={t("sandbox.reclaimRuntime", { id })}
                      >
                        <Trash2 size={14} aria-hidden="true" />
                        {pendingId === id ? t("sandbox.reclaiming") : t("sandbox.reclaim")}
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
