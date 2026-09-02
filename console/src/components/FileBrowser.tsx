import { ChevronRight, File, Folder, LoaderCircle, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import type { WorkspaceFileEntry, WorkspaceReadView } from "../types";

/**
 * Read-only browser for one workspace.
 *
 * Supports listing directories and reading text files. It intentionally has no
 * write, delete, or download action: agents own all workspace writes.
 *
 * Access follows the caller's credential. Control Plane accepts it only for the read subset
 * of the files API; workspace-scoped write tokens must never be delivered to browsers.
 * Directory entries may be truncated at File Service's MAX_LIST_ENTRIES, and that
 * state must be shown rather than implied to be the complete list.
 *
 * The parent keys this component by workspace id, so every workspace starts at
 * its root with nothing open; do not add a `workspaceId` effect that resets state.
 */

function parentOf(path: string): string {
  if (path === "." || !path.includes("/")) {
    return ".";
  }
  return path.slice(0, path.lastIndexOf("/"));
}

function joinPath(base: string, name: string): string {
  return base === "." ? name : `${base}/${name}`;
}

export default function FileBrowser({
  workspaceId,
  onClose,
}: {
  workspaceId: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [path, setPath] = useState(".");
  const [entries, setEntries] = useState<WorkspaceFileEntry[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [file, setFile] = useState<WorkspaceReadView | null>(null);
  // Path of the file whose first page is being fetched; drives the pending row.
  const [reading, setReading] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Only the newest read may touch state. Two quick clicks on different files
  // would otherwise show whichever response arrived last, under the other name.
  const readTicket = useRef(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.listWorkspaceFiles(workspaceId, path)
      .then((view) => {
        if (cancelled) {
          return;
        }
        setEntries(view.entries);
        setTruncated(view.truncated);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, path]);

  // Escape closes the browser, matching what the close button does.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const navigate = useCallback((target: string) => {
    readTicket.current += 1;
    setReading(null);
    setLoadingMore(false);
    setFile(null);
    setPath(target);
  }, []);

  const openFile = useCallback(
    (target: string) => {
      const ticket = ++readTicket.current;
      setReading(target);
      setLoadingMore(false);
      setError(null);
      api.readWorkspaceFile(workspaceId, target)
        .then((view) => {
          if (ticket === readTicket.current) {
            setFile(view);
          }
        })
        .catch((cause: unknown) => {
          if (ticket === readTicket.current) {
            setFile(null);
            setError(cause instanceof Error ? cause.message : String(cause));
          }
        })
        .finally(() => {
          if (ticket === readTicket.current) {
            setReading(null);
          }
        });
    },
    [workspaceId],
  );

  const openEntry = useCallback(
    (entry: WorkspaceFileEntry) => {
      const next = joinPath(path, entry.name);
      if (entry.type === "directory") {
        navigate(next);
        return;
      }
      if (entry.type !== "file") {
        return;
      }
      openFile(next);
    },
    [navigate, openFile, path],
  );

  /**
   * Fetch the next page of the open file and append it. `next_offset` is the
   * contract, not `end_line + 1`: after a hard-clipped line File Service points
   * past that line, and the two differ exactly there.
   */
  const loadMore = useCallback(() => {
    if (!file || file.next_offset === undefined) {
      return;
    }
    const ticket = ++readTicket.current;
    const current = file;
    setLoadingMore(true);
    setError(null);
    api.readWorkspaceFile(workspaceId, current.path, current.next_offset)
      .then((view) => {
        if (ticket !== readTicket.current) {
          return;
        }
        // Lines keep their newline, so pages join as-is. A clipped line has none.
        const separator = current.content.endsWith("\n") || !current.content ? "" : "\n";
        setFile({
          ...view,
          content: `${current.content}${separator}${view.content}`,
          start_line: current.start_line,
        });
      })
      .catch((cause: unknown) => {
        if (ticket === readTicket.current) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      })
      .finally(() => {
        if (ticket === readTicket.current) {
          setLoadingMore(false);
        }
      });
  }, [file, workspaceId]);

  return (
    <section className="card" aria-labelledby="files-title">
      <div className="card-header">
        <h2 className="card-title" id="files-title">
          <Folder size={18} aria-hidden="true" />
          <span className="mono">{t("files.title", { id: workspaceId })}</span>
        </h2>
        <button
          type="button"
          className="button button-small"
          onClick={onClose}
          aria-label={t("files.closeBrowser")}
        >
          <X size={14} aria-hidden="true" />
          {t("common.close")}
        </button>
      </div>

      <div className="breadcrumb">
        <button type="button" onClick={() => navigate(".")}>
          workspace
        </button>
        {path !== "." &&
          path.split("/").map((segment, index, all) => {
            const target = all.slice(0, index + 1).join("/");
            return (
              <span key={target}>
                <ChevronRight size={12} aria-hidden="true" />
                <button type="button" onClick={() => navigate(target)}>
                  {segment}
                </button>
              </span>
            );
          })}
      </div>

      {error ? (
        <div className="error-banner" role="alert">
          <TriangleAlert size={16} aria-hidden="true" />
          {error}
        </div>
      ) : null}

      <div className="files-layout">
        <div>
          {loading ? (
            <p className="empty">{t("common.loading")}</p>
          ) : (
            <ul className="file-list">
              {path !== "." ? (
                <li>
                  <button
                    type="button"
                    className="file-entry"
                    onClick={() => navigate(parentOf(path))}
                  >
                    <Folder size={14} aria-hidden="true" />
                    ..
                  </button>
                </li>
              ) : null}
              {entries.map((entry) => {
                const target = joinPath(path, entry.name);
                const pending = reading === target;
                return (
                  <li key={entry.name}>
                    <button
                      type="button"
                      className="file-entry"
                      onClick={() => openEntry(entry)}
                      aria-current={file?.path === target || pending}
                      aria-busy={pending}
                      disabled={entry.type === "other"}
                    >
                      {pending ? (
                        <LoaderCircle className="spin" size={14} aria-hidden="true" />
                      ) : entry.type === "directory" ? (
                        <Folder size={14} aria-hidden="true" />
                      ) : (
                        <File size={14} aria-hidden="true" />
                      )}
                      {entry.name}
                    </button>
                  </li>
                );
              })}
              {entries.length === 0 ? (
                <li className="empty">{t("files.emptyDirectory")}</li>
              ) : null}
            </ul>
          )}
          {truncated ? (
            <p className="notice">
              <TriangleAlert size={14} aria-hidden="true" />
              {t("files.truncatedDirectory")}
            </p>
          ) : null}
        </div>

        <div>
          {file ? (
            <>
              <p className="refresh-note">
                <span className="mono">{file.path}</span> ·{" "}
                {t("files.lines", {
                  start: file.start_line,
                  end: file.end_line,
                })}
                {file.truncated ? t("files.incompleteRead") : ""}
              </p>
              <pre className="file-content">{file.content}</pre>
              {file.clipped_line ? (
                <p className="notice">
                  <TriangleAlert size={14} aria-hidden="true" />
                  {t("files.clippedLine", {
                    line: file.clipped_line,
                    length: file.clipped_length ?? 0,
                  })}
                </p>
              ) : null}
              {file.truncated && file.next_offset !== undefined ? (
                <p className="form-actions">
                  <button
                    type="button"
                    className="button button-small"
                    disabled={loadingMore}
                    aria-busy={loadingMore}
                    onClick={loadMore}
                  >
                    {loadingMore ? (
                      <LoaderCircle className="spin" size={14} aria-hidden="true" />
                    ) : null}
                    {t("files.loadMore")}
                  </button>
                </p>
              ) : null}
            </>
          ) : reading ? (
            <p className="empty">{t("common.loading")}</p>
          ) : (
            <p className="empty">{t("files.selectFile")}</p>
          )}
        </div>
      </div>
    </section>
  );
}
