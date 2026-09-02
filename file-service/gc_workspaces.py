#!/usr/bin/env python3
"""Retention-based garbage collection for inactive Workspace PVC directories."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path


WORKSPACE_ID = re.compile(r"^ws-[a-f0-9]{12}$")

# Tolerance left for "the value can only come from the current write": the writer (file_service.record_activity) writes
# The node-level deviation between your own time.time() and the GC's clock is seconds; 24h is deliberately generous
# value, which separates the clock problem from the forgery problem in terms of criteria - values beyond it have no legitimate explanation.
LAST_USED_MAX_AHEAD_SECONDS = 24 * 3600


def last_used_at(workspace: Path, *, now: int) -> int:
    marker = workspace / ".sandbox" / "last_used_at"
    ceiling = now + LAST_USED_MAX_AHEAD_SECONDS
    try:
        value = int(marker.read_text(encoding="ascii").strip())
        # AI-LOCK: The marker falls on a file system that is directly writable by the tenant (sandbox shell and
        # file-service is the same as subPath and uid), the value here is an untrusted input and cannot be just verified
        # Accept "> 0" - just `echo 9999999999 > last_used_at` will make the data
        # Permanently escapes TTL recycling (cost-side vulnerability). Values that fall into the future range are treated as forgeries:
        # Falling back to the mtime of the marker itself. Forging this action itself indicates that the tenant has just been active.
        # mtime is the only moment in the relationship that is not written by the pen of the tenant. mtime can also be
        # `touch -d 2030-01-01` pushes to the future, so the fallback value is also capped at ceiling——
        # The upper limit of fake income is reduced from "permanent" to "last real contact + TTL + 24h".
        # And renewing the forgery requires the sandbox pod to be alive.
        if value > 0:
            if value <= ceiling:
                return value
            print(
                f"[gc] {workspace.name}: last_used_at {value} is beyond "
                f"now+{LAST_USED_MAX_AHEAD_SECONDS}s; treating as tenant-"
                f"forged and falling back to marker mtime",
                file=sys.stderr,
                flush=True,
            )
            return min(int(marker.stat().st_mtime), ceiling)
    except (OSError, ValueError):
        pass
    return int(workspace.stat().st_mtime)


def collect_expired(
    root: Path,
    *,
    now: int,
    ttl_seconds: int,
) -> list[Path]:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be greater than zero")
    expired: list[Path] = []
    for candidate in sorted(root.iterdir()):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not WORKSPACE_ID.fullmatch(candidate.name)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
        ):
            continue
        if last_used_at(candidate, now=now) + ttl_seconds <= now:
            expired.append(candidate)
    return expired


def purge_workspace(root: Path, workspace: Path) -> dict[str, object]:
    root = root.resolve()
    if workspace.parent.resolve() != root or not WORKSPACE_ID.fullmatch(workspace.name):
        raise ValueError("workspace path is outside the configured root")
    info = workspace.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("workspace path is not a directory")
    # rmtree does not follow directory symbolic links (Python 3.12: when encountering a symlink, it will raise instead of deleting it),
    # Therefore, tenants cannot delete the host using `ln -s / mnt` in their own Workspace. This depends on
    # Standard library behavior rather than checking of this function - the lstat above only blocks "the entire Workspace itself
    # It is a symbolic link" and cannot block the internal ones. Written down here because this assumption was not obvious during review.
    # Don't replace it with os.walk without equivalent protection and delete it yourself.
    shutil.rmtree(workspace)
    return {"workspace_id": workspace.name, "deleted": True}


def main() -> int:
    root = Path(os.getenv("WORKSPACE_GC_ROOT", "/workspaces")).resolve()
    ttl_seconds = int(os.getenv("WORKSPACE_DATA_TTL_SECONDS", "2592000"))
    dry_run = os.getenv("WORKSPACE_GC_DRY_RUN", "false").lower() in {
        "1", "true", "yes",
    }
    now = int(time.time())
    candidates = collect_expired(root, now=now, ttl_seconds=ttl_seconds)
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for workspace in candidates:
        # Isolate one by one: a Workspace cannot be deleted (permissions, EIO, being held by other processes, or
        # It was replaced by a symbolic link between collect and purge) and should not allow hundreds of subsequent ones to be deleted.
        # Without this layer, the actual cleaning amount of a GC depends on the first bad directory in sorted().
        # Which number - and it gets stuck in the same place, in the same order, every hour.
        try:
            if not dry_run:
                purge_workspace(root, workspace)
        except (OSError, ValueError) as exc:
            failed.append({
                "workspace_id": workspace.name,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        deleted.append(workspace.name)
    print(json.dumps({
        "status": "ok" if not failed else "partial",
        "dry_run": dry_run,
        "ttl_seconds": ttl_seconds,
        "candidates": len(candidates),
        "workspaces": deleted,
        "failed": failed,
    }, sort_keys=True), flush=True)
    # If there is a failure, exit non-zero. In addition to the log, CronJob only has the exit code as a signal, and the "cleanup volume is silently
    # "Reduce" is exactly the kind of fault that no one will take the initiative to look at. backoffLimit: 1 will rerun, delete itself
    # It is idempotent (the dead Workspace will not even be a candidate in the next round).
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
