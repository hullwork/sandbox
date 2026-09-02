import { ChevronRight, File, Folder, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
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
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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

  const openEntry = useCallback(
    (entry: WorkspaceFileEntry) => {
      const next = joinPath(path, entry.name);
      if (entry.type === "directory") {
        setFile(null);
        setPath(next);
        return;
      }
      if (entry.type !== "file") {
        return;
      }
      api.readWorkspaceFile(workspaceId, next)
        .then((view) => {
          setFile(view);
          setError(null);
        })
        .catch((cause: unknown) => {
          setFile(null);
          setError(cause instanceof Error ? cause.message : String(cause));
        });
    },
    [path, workspaceId],
  );

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
        <button type="button" onClick={() => {
          setPath(".");
          setFile(null);
        }}>
          workspace
        </button>
        {path !== "." &&
          path.split("/").map((segment, index, all) => {
            const target = all.slice(0, index + 1).join("/");
            return (
              <span key={target}>
                <ChevronRight size={12} aria-hidden="true" />
                <button
                  type="button"
                  onClick={() => {
                    setPath(target);
                    setFile(null);
                  }}
                >
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
                    onClick={() => {
                      setPath(parentOf(path));
                      setFile(null);
                    }}
                  >
                    <Folder size={14} aria-hidden="true" />
                    ..
                  </button>
                </li>
              ) : null}
              {entries.map((entry) => (
                <li key={entry.name}>
                  <button
                    type="button"
                    className="file-entry"
                    onClick={() => openEntry(entry)}
                    aria-current={file?.path === joinPath(path, entry.name)}
                    disabled={entry.type === "other"}
                  >
                    {entry.type === "directory" ? (
                      <Folder size={14} aria-hidden="true" />
                    ) : (
                      <File size={14} aria-hidden="true" />
                    )}
                    {entry.name}
                  </button>
                </li>
              ))}
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
            </>
          ) : (
            <p className="empty">{t("files.selectFile")}</p>
          )}
        </div>
      </div>
    </section>
  );
}
