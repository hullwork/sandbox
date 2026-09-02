import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import type { HealthView, MonitoringView, SandboxView, WorkspaceView } from "../types";
import FileBrowser from "./FileBrowser";
import HealthCard from "./HealthCard";
import MonitoringPanel from "./MonitoringPanel";
import { RefreshButton } from "./RefreshButton";
import SandboxTable from "./SandboxTable";
import WorkspaceTable from "./WorkspaceTable";

/**
 * Sandbox operations page.
 *
 * The page is read-only monitoring for Control Plane, runtimes, and workspaces, plus
 * individual reclaim actions. It never creates sandboxes: sessions and backend
 * ownership always drive creation.
 *
 * Control Plane scopes `scope_sandboxes` and `scope_workspaces` according to credentials.
 * The frontend intentionally does no additional filtering.
 */

const REFRESH_INTERVAL_MS = 5000;

export default function OverviewView({
  onError,
}: {
  onError: (cause: unknown) => void;
}) {
  const { formatTime, t } = useI18n();
  const [health, setHealth] = useState<HealthView | null>(null);
  const [monitoring, setMonitoring] = useState<MonitoringView | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxView[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceView[]>([]);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  // Remember whether the last refresh failed. Health must downgrade to “unknown”;
  // continuing to display a previous green state would be more dangerous than no UI.
  const [stale, setStale] = useState(false);
  const [pendingSandbox, setPendingSandbox] = useState<string | null>(null);
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState<string | null>(null);
  // Do not show an empty list before the first response; doing so would imply that
  // every sandbox has disappeared.
  const loadedOnce = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [nextHealth, nextMonitoring, nextWorkspaces] = await Promise.all([
        api.getHealth(),
        api.getMonitoring(),
        api.listWorkspaces(),
      ]);
      setHealth(nextHealth);
      setMonitoring(nextMonitoring);
      // Monitoring Runtime rows are a strict superset of SandboxView. Reusing
      // them avoids listing the same Pods twice on every five-second refresh.
      setSandboxes(nextMonitoring.runtimes);
      setWorkspaces(nextWorkspaces);
      setStale(false);
      setRefreshedAt(new Date());
      loadedOnce.current = true;
    } catch (cause) {
      // Keep the last timestamp on failure. It describes when data was captured;
      // refreshing it would falsely claim current data.
      setStale(true);
      onError(cause);
    }
  }, [onError]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleDeleteSandbox = useCallback(
    async (id: string) => {
      if (!window.confirm(t("overview.confirmReclaimRuntime", { id }))) {
        return;
      }
      setPendingSandbox(id);
      try {
        await api.deleteSandbox(id);
        await refresh();
      } catch (cause) {
        onError(cause);
      } finally {
        setPendingSandbox(null);
      }
    },
    [onError, refresh, t],
  );

  const handleDeleteWorkspace = useCallback(
    async (id: string) => {
      if (!window.confirm(t("overview.confirmDeleteWorkspace", { id }))) {
        return;
      }
      setPendingWorkspace(id);
      try {
        await api.deleteWorkspace(id);
        if (browsing === id) {
          setBrowsing(null);
        }
        await refresh();
      } catch (cause) {
        onError(cause);
      } finally {
        setPendingWorkspace(null);
      }
    },
    [browsing, onError, refresh, t],
  );

  return (
    <div className="page">
      <div className="page-toolbar">
        <span className="refresh-note" aria-live="polite">
          {refreshedAt
            ? t(stale ? "overview.staleDataAsOf" : "overview.dataAsOf", {
              time: formatTime(refreshedAt),
            })
            : t("overview.notLoaded")}
          {t("overview.autoRefresh", {
            seconds: REFRESH_INTERVAL_MS / 1000,
          })}
        </span>
        <RefreshButton onRefresh={refresh}>
          {t("overview.refreshNow")}
        </RefreshButton>
      </div>

      <HealthCard health={health} stale={stale} />

      {monitoring ? <MonitoringPanel monitoring={monitoring} /> : null}

      {loadedOnce.current || sandboxes.length > 0 ? (
        <SandboxTable
          sandboxes={sandboxes}
          onDelete={(id) => void handleDeleteSandbox(id)}
          pendingId={pendingSandbox}
        />
      ) : null}

      {loadedOnce.current || workspaces.length > 0 ? (
        <WorkspaceTable
          workspaces={workspaces}
          onDelete={(id) => void handleDeleteWorkspace(id)}
          onBrowse={setBrowsing}
          pendingId={pendingWorkspace}
          browsingId={browsing}
        />
      ) : null}

      {browsing ? (
        <FileBrowser workspaceId={browsing} onClose={() => setBrowsing(null)} />
      ) : null}
    </div>
  );
}
