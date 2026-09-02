import { Activity, Server, TriangleAlert } from "lucide-react";
import { useI18n } from "../i18n";
import type {
  CpuMonitoring,
  MemoryMonitoring,
  MetricAvailability,
  MonitoringView,
} from "../types";

function formatCpu(value: number | null | undefined): string {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)} CPU` : `${value}m`;
}

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(amount >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function ResourceValue({
  usage,
  denominator,
  secondary,
  formatter,
}: {
  usage: number | null;
  denominator: number | null | undefined;
  secondary: string;
  formatter: (value: number | null | undefined) => string;
}) {
  const percent = usage != null && denominator && denominator > 0
    ? Math.round((usage / denominator) * 100)
    : null;
  return (
    <div className="resource-value">
      <span className="resource-primary">
        {usage == null ? "—" : `${formatter(usage)}${percent == null ? "" : ` · ${percent}%`}`}
      </span>
      {percent != null ? (
        <progress max={100} value={Math.min(100, percent)} aria-label={`${percent}%`} />
      ) : null}
      <span className="resource-secondary">{secondary}</span>
    </div>
  );
}

function MetricsNotice({ state }: { state: MetricAvailability | null }) {
  const { t } = useI18n();
  if (!state || state.available) return null;
  return (
    <div className="monitoring-notice" role="status">
      <TriangleAlert size={16} aria-hidden="true" />
      <span>{t(`monitoring.metrics.${state.reason ?? "metrics_api_error"}`)}</span>
    </div>
  );
}

function nodeCpu(cpu: CpuMonitoring, allocatable: string) {
  return {
    denominator: cpu.allocatable_millicores,
    secondary: `${allocatable} ${formatCpu(cpu.allocatable_millicores)} / ${formatCpu(cpu.capacity_millicores)}`,
  };
}

function nodeMemory(memory: MemoryMonitoring, allocatable: string) {
  return {
    denominator: memory.allocatable_bytes,
    secondary: `${allocatable} ${formatBytes(memory.allocatable_bytes)} / ${formatBytes(memory.capacity_bytes)}`,
  };
}

export default function MonitoringPanel({ monitoring }: { monitoring: MonitoringView }) {
  const { t, tPlural } = useI18n();
  return (
    <div className="monitoring-stack">
      {monitoring.nodes_visible ? (
        <section className="card" aria-labelledby="node-monitoring-title">
          <div className="card-header">
            <h2 className="card-title" id="node-monitoring-title">
              <Server size={18} aria-hidden="true" />
              {t("monitoring.nodes.title")}
            </h2>
            <span className="card-count">{tPlural("monitoring.nodes.count", monitoring.nodes.length)}</span>
          </div>
          <MetricsNotice state={monitoring.metrics.nodes} />
          {monitoring.nodes.length === 0 ? (
            <p className="state">{t("monitoring.nodes.empty")}</p>
          ) : (
            <div className="table-scroll">
              <table>
                <caption className="visually-hidden">{t("monitoring.nodes.caption")}</caption>
                <thead><tr>
                  <th>{t("monitoring.node")}</th>
                  <th>{t("common.status")}</th>
                  <th>{t("monitoring.cpu")}</th>
                  <th>{t("monitoring.memory")}</th>
                  <th>{t("monitoring.node.details")}</th>
                </tr></thead>
                <tbody>{monitoring.nodes.map((node) => {
                  const cpu = nodeCpu(node.cpu, t("monitoring.allocatable"));
                  const memory = nodeMemory(node.memory, t("monitoring.allocatable"));
                  return <tr key={node.name}>
                    <td><strong className="mono">{node.name}</strong><div className="subtle">{node.roles.join(", ")}</div></td>
                    <td><span className={`badge ${node.status === "ready" && !node.unschedulable ? "badge-ok" : "badge-warn"}`}>
                      {node.unschedulable ? t("monitoring.unschedulable") : t(`monitoring.node.${node.status}`)}
                    </span></td>
                    <td><ResourceValue usage={node.cpu.usage_millicores} denominator={cpu.denominator} secondary={cpu.secondary} formatter={formatCpu} /></td>
                    <td><ResourceValue usage={node.memory.usage_bytes} denominator={memory.denominator} secondary={memory.secondary} formatter={formatBytes} /></td>
                    <td><span className="mono">{node.kubelet_version ?? "—"}</span><div className="subtle">{node.architecture ?? "—"} · {node.pod_capacity} pods</div></td>
                  </tr>;
                })}</tbody>
              </table>
            </div>
          )}
        </section>
      ) : null}

      <section className="card" aria-labelledby="runtime-monitoring-title">
        <div className="card-header">
          <h2 className="card-title" id="runtime-monitoring-title">
            <Activity size={18} aria-hidden="true" />
            {t("monitoring.runtimes.title")}
          </h2>
          <span className="card-count">{tPlural("monitoring.runtimes.count", monitoring.runtimes.length)}</span>
        </div>
        <MetricsNotice state={monitoring.metrics.runtimes} />
        {monitoring.runtimes.length === 0 ? (
          <p className="state">{t("monitoring.runtimes.empty")}</p>
        ) : (
          <div className="table-scroll">
            <table>
              <caption className="visually-hidden">{t("monitoring.runtimes.caption")}</caption>
              <thead><tr>
                <th>{t("sandbox.title")}</th>
                <th>{t("common.status")}</th>
                {monitoring.nodes_visible ? <th>{t("monitoring.node")}</th> : null}
                <th>{t("monitoring.cpu")}</th>
                <th>{t("monitoring.memory")}</th>
                <th>{t("monitoring.restarts")}</th>
              </tr></thead>
              <tbody>{monitoring.runtimes.map((runtime) => <tr key={runtime.id ?? runtime.workspace_id}>
                <td><strong className="mono">{runtime.id ?? "—"}</strong><div className="subtle mono">{runtime.workspace_id ?? "—"}</div></td>
                <td><span className={`badge ${runtime.ready ? "badge-ok" : "badge-warn"}`}>{runtime.status}</span></td>
                {monitoring.nodes_visible ? <td className="mono">{runtime.node ?? "—"}</td> : null}
                <td><ResourceValue usage={runtime.cpu.usage_millicores} denominator={runtime.cpu.limit_millicores} secondary={`${t("monitoring.request")} ${formatCpu(runtime.cpu.request_millicores)} · ${t("monitoring.limit")} ${formatCpu(runtime.cpu.limit_millicores)}`} formatter={formatCpu} /></td>
                <td><ResourceValue usage={runtime.memory.usage_bytes} denominator={runtime.memory.limit_bytes} secondary={`${t("monitoring.request")} ${formatBytes(runtime.memory.request_bytes)} · ${t("monitoring.limit")} ${formatBytes(runtime.memory.limit_bytes)}`} formatter={formatBytes} /></td>
                <td className="mono">{runtime.restarts}</td>
              </tr>)}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
