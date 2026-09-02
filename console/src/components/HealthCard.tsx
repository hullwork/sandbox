import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  HeartPulse,
  Unplug,
} from "lucide-react";
import { useI18n } from "../i18n";
import type { HealthView } from "../types";

/**
 * Three-state display for Control Plane `/healthz`.
 *
 * The component exposes Control Plane's health JSON as returned and never recomputes the
 * overall health decision.
 *
 * `unchecked` must not be rendered as a pass. Control Plane deliberately distinguishes it
 * for managed S3 services that expose no anonymously reachable health endpoint.
 * If a refresh fails, the previous green state must be downgraded to unknown.
 */

function StatusBadge({
  value,
  stale,
}: {
  value: string | undefined;
  stale: boolean;
}) {
  const { t } = useI18n();

  if (stale) {
    return (
      <span className="badge badge-muted">
        <Unplug size={14} aria-hidden="true" />
        {t("health.unknown")}
      </span>
    );
  }
  if (value === "ok") {
    return (
      <span className="badge badge-ok">
        <CircleCheck size={14} aria-hidden="true" />
        {t("health.ok")}
      </span>
    );
  }
  if (value === "unchecked") {
    return (
      <span className="badge badge-warn">
        <CircleHelp size={14} aria-hidden="true" />
        {t("health.unchecked")}
      </span>
    );
  }
  return (
    <span className="badge badge-bad">
      <CircleAlert size={14} aria-hidden="true" />
      {value ?? t("health.failed")}
    </span>
  );
}

export default function HealthCard({
  health,
  stale,
}: {
  health: HealthView | null;
  stale: boolean;
}) {
  const { t } = useI18n();

  return (
    <section className="card" aria-labelledby="health-title">
      <div className="card-header">
        <h2 className="card-title" id="health-title">
          <HeartPulse size={18} aria-hidden="true" />
          {t("health.title")}
        </h2>
      </div>
      {health === null ? (
        <p className="empty">{t("common.loading")}</p>
      ) : (
        <>
          <div className="health-grid">
            <div className="health-item">
              <span className="health-label">{t("health.overall")}</span>
              <span className="health-value">
                <StatusBadge value={health.error ? undefined : health.status} stale={stale} />
              </span>
            </div>
            <div className="health-item">
              <span className="health-label">{t("health.kubernetes")}</span>
              <span className="health-value">
                <StatusBadge
                  value={health.error ? undefined : health.kubernetes}
                  stale={stale}
                />
              </span>
            </div>
            <div className="health-item">
              <span className="health-label">{t("health.objectStorage")}</span>
              <span className="health-value">
                <StatusBadge
                  value={health.error ? undefined : health.object_storage}
                  stale={stale}
                />
              </span>
            </div>
          </div>
          {stale ? (
            <p className="notice" role="status">
              <Unplug size={14} aria-hidden="true" />
              {t("health.stale")}
            </p>
          ) : null}
          {!stale && health.error ? (
            <div className="diagnosis" role="alert">
              <strong>{health.error}</strong>
              {health.endpoint ? (
                <span>
                  {t("health.endpoint", {
                    endpoint: health.endpoint,
                  })}
                </span>
              ) : null}
              {health.diagnosis ? <span>{health.diagnosis}</span> : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
