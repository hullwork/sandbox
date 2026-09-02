import { useState } from "react";
import { useI18n } from "../i18n";
import type { GrafanaCapability } from "../types";

/**
 * Embedded Grafana panels, rendered as same-origin iframes.
 *
 * 🔴 Why an iframe to our own origin rather than to Grafana: a cross-origin
 * frame would need `frame-src` opened in the Console CSP, `allow_embedding` and
 * `cookie_samesite=none` on the Grafana side, and would leave a Grafana
 * credential in the browser. Control Plane proxies the panel instead and holds the
 * service-account token server-side, so the Console CSP is untouched.
 *
 * 🔴 Why the URL is assembled here from a fixed catalog and a fixed range list:
 * whatever this component puts in the query string is what Control Plane forwards to
 * Grafana. A free-form range or panel id would hand Grafana URL construction to
 * the page. Panel ids come from `/v1/whoami`, which reads them from the shipped
 * dashboard; the range is one of the three literals below.
 *
 * This is an operator view. The metrics behind these panels carry no tenant
 * dimension by design, so every panel is cross-tenant data - Control Plane returns the
 * `grafana` block only to an administrator and re-checks on every panel request.
 * Hiding the tab is presentation, never the access decision.
 */

type RangeId = "6h" | "24h" | "7d";

const RANGES: ReadonlyArray<{ id: RangeId; from: string }> = [
  { id: "6h", from: "now-6h" },
  { id: "24h", from: "now-24h" },
  { id: "7d", from: "now-7d" },
];

const RANGE_LABEL: Record<RangeId, "observability.range.6h" | "observability.range.24h" | "observability.range.7d"> = {
  "6h": "observability.range.6h",
  "24h": "observability.range.24h",
  "7d": "observability.range.7d",
};

export default function ObservabilityView({
  grafana,
}: {
  grafana: GrafanaCapability;
}) {
  const { t } = useI18n();
  const [range, setRange] = useState<RangeId>("6h");
  const panels = grafana.panels ?? [];
  const route = grafana.route ?? "/grafana/";
  const uid = grafana.dashboardUid ?? "";
  const from = RANGES.find((item) => item.id === range)?.from ?? "now-6h";

  return (
    <div className="page">
      <section className="card">
        <div className="card-header">
          <h2 className="card-title">{t("observability.title")}</h2>
          <div className="header-actions">
            <label className="field field-inline">
              <span>{t("observability.rangeLabel")}</span>
              <select
                value={range}
                onChange={(event) => setRange(event.target.value as RangeId)}
              >
                {RANGES.map((item) => (
                  <option key={item.id} value={item.id}>
                    {t(RANGE_LABEL[item.id])}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
        <p className="panel-note">{t("observability.subtitle")}</p>
        <p className="panel-note">{t("observability.crossTenantNotice")}</p>
        {panels.length && uid ? (
          <div className="panel-grid">
            {panels.map((panel) => {
              const query = new URLSearchParams({
                panelId: String(panel.id),
                from,
                to: "now",
                theme: "light",
              });
              return (
                <iframe
                  key={panel.id}
                  className="panel-frame"
                  title={t("observability.panelLabel", { title: panel.title })}
                  src={`${route}d-solo/${encodeURIComponent(uid)}?${query.toString()}`}
                  loading="lazy"
                  // Nothing in a chart needs top-level navigation, popups or
                  // form submission; the frame only has to run Grafana's bundle.
                  sandbox="allow-scripts allow-same-origin"
                  referrerPolicy="no-referrer"
                />
              );
            })}
          </div>
        ) : (
          <p className="empty">{t("observability.unavailable")}</p>
        )}
      </section>
    </div>
  );
}
