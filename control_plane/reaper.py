#!/usr/bin/env python3
"""Cleanup actions for expired runtimes, workspaces, and storage leases.

This module decides what should be removed and performs the deletion. The
process entrypoint in ``server.py`` owns scheduling and shutdown.
"""
from __future__ import annotations

from .store import (
    StoreError,
)
import contextlib
from datetime import datetime
import json
import time

from . import core as control_plane

__all__ = (
    "reap_expired_checkpoints",
    "reap_expired_ticket_leases",
    "reap_once",
)


def reap_expired_checkpoints(now: int | None = None) -> int:
    current = now or int(time.time())
    prefix = "workspaces/"
    output = control_plane.run_mc(
        "ls",
        "--recursive",
        "--json",
        f"{control_plane.MC_ALIAS}/{control_plane.OBJECT_STORE_WORKSPACE_BUCKET}/{prefix}",
        max_output_bytes=8 * 1024 * 1024,
    )
    removed = 0
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        try:
            item = json.loads(raw_line)
            listed_key = str(item.get("key") or "")
            key = (
                listed_key
                if listed_key.startswith(prefix)
                else f"{prefix}{listed_key}"
            )
            modified = str(item.get("lastModified") or "")
            modified_at = int(
                datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if (
            "/checkpoints/" not in key
            or not key.endswith(".tar.gz")
            or modified_at + control_plane.CHECKPOINT_RETENTION_SECONDS > current
        ):
            continue
        control_plane.run_mc(
            "rm",
            "--versions",
            "--force",
            f"{control_plane.MC_ALIAS}/{control_plane.OBJECT_STORE_WORKSPACE_BUCKET}/{key}",
        )
        removed += 1
    return removed


def reap_once(now: int | None = None) -> dict[str, int]:
    current = now or int(time.time())
    runtime_driver = control_plane.configured_runtime_driver()
    runtimes = runtime_driver.list_runtimes()
    active_workspaces: set[str] = set()
    removed_runtimes = 0
    reprieved_runtimes = 0
    # Reconciliation must use the Runtimes still alive after this round's scan, not the batch fetched at the start -
    # a Runtime deleted by TTL mid-round would otherwise still be in the list with its store row already released,
    # and the quota delete_runtime just returned would be reconciled a second time.
    surviving: list[tuple[str, str]] = []
    for runtime in runtimes:
        expires_at = runtime.expires_at or 0
        hard_expires_at = runtime.hard_expires_at or 0
        sandbox_id = runtime.runtime_id
        workspace_id = runtime.workspace_id
        if sandbox_id and expires_at and expires_at <= current:
            # Double-check before acting: the Control Plane only sees requests that pass through it, and long-running work
            # started inside the sandbox (async exec, PTY sessions) generates no MCP calls. Going by expires-at
            # alone would delete a Runtime that is busy right now, work included.
            #
            # The reprieve cannot be extended forever, though - check the absolute ceiling first. hard-expires-at is
            # fixed at creation and no touch pushes it forward. Past it the Runtime is deleted without asking;
            # otherwise a `while true` would truthfully report busy forever and make the sandbox immortal.
            over_hard_limit = bool(
                hard_expires_at and hard_expires_at <= current
            )
            if not over_hard_limit and control_plane.probe_runtime_busy(sandbox_id):
                control_plane.touch_runtime(sandbox_id, current)
                reprieved_runtimes += 1
                if workspace_id:
                    active_workspaces.add(workspace_id)
                continue
            control_plane.delete_runtime(sandbox_id)
            removed_runtimes += 1
            continue
        elif workspace_id:
            active_workspaces.add(workspace_id)
        if sandbox_id:
            surviving.append((sandbox_id, runtime.tenant_id or ""))

    reconciled_rows = 0
    reconciled_orphans = 0
    stale_pending = 0
    if control_plane.STORE is not None:
        # 🔴 The order of the three steps matters:
        #   1. Clear stuck pending rows first - they never had a Pod, and if mixed into direction one they would
        #      read as "row in the store, nothing in the cluster", which is also exactly what a sandbox still being
        #      provisioned looks like. Handled separately so the criteria do not interfere (this one is by age,
        #      the one below by status).
        #   2. Direction one looks only at active: active means the Pod was ready once and is now gone - really
        #      gone (node died, kubectl delete, Control Plane crashed before rolling back).
        #   3. Direction two only touches Pods carrying a tenant label: a Pod without one is either a single-tenant
        #      mode sandbox (never in the store to begin with) or was provisioned before this change shipped, and
        #      must not be deleted as an orphan.
        try:
            stale_pending = control_plane.STORE.release_stale_pending_runtimes(
                control_plane.PENDING_STALE_SECONDS
            )
            live_rows = control_plane.STORE.list_live_runtimes()
        except StoreError as exc:
            # A store outage must not interrupt runtime TTL collection - that is the part that burns resources.
            print(f"[reaper] runtime reconcile skipped: {exc}", flush=True)
            live_rows = None
        if live_rows is not None:
            # 🔴 The two directions want **different snapshots**:
            # Direction one asks "is it in the cluster right now?" and must re-list. The list from the start of the
            # round predates this round's scan, and the scan (≤2s per expired Pod, the busy probe adds two more K8s
            # round trips) is long enough for a new sandbox to go through admit → provision Pod → activate. Judging
            # "row in the store, nothing in the cluster" against the old snapshot would write that row off, hand its
            # quota out again, and let direction two delete the Pod next round. What the user sees is a sandbox
            # that ran for half a minute and vanished.
            # Direction two asks "is this Pod an orphan?" and must use surviving instead: those Pods existed before
            # this round started, so their rows should long since be recorded; switching to the fresh snapshot
            # would delete Pods whose rows were written after live_rows was read as orphans - worse than the
            # problem it is meant to solve.
            try:
                fresh_runtimes = runtime_driver.list_runtimes()
            except control_plane.RuntimeDriverError as exc:
                # If the re-list fails, nothing is written off. Direction one is destructive, and the right behavior
                # when the data is unavailable is to do nothing rather than judge on data known to be stale.
                print(f"[reaper] runtime reconcile skipped: {exc}", flush=True)
                fresh_runtimes = None
            if fresh_runtimes is not None:
                runtime_ids = {
                    runtime.runtime_id for runtime in fresh_runtimes
                }
                for row in live_rows:
                    if row["status"] != "active":
                        continue
                    if row["sandbox_id"] in runtime_ids:
                        continue
                    # 🔴 A vanished Pod does not mean the Runtime is gone: the Service is a separate object, and
                    # whatever deleted the Pod (node gone, kubectl delete pod, eviction) does not take it along.
                    # Once this row is written off it is no longer live: next round direction one cannot see it,
                    # and direction two only has Pods in its surviving list - the Service is never looked at
                    # again and holds its ClusterIP forever. Enough churn drains the Service CIDR, and then no new
                    # sandbox can be provisioned even though the Pod quota is plainly still available.
                    # This is the only moment it is seen, so it must be collected before the row is written off.
                    # Order: delete the Service first, then return the quota. The other way round, a failed delete
                    # would also lose the only clue (the active row); this way a failure fails conservatively -
                    # the slot stays taken one more round and the next attempt comes 15 seconds later.
                    # As in delete_runtime, KUBE.delete treats 404 as success, so the common path of "the Service
                    # is already gone" does not cost an extra failure.
                    runtime_driver.delete_endpoint(row["sandbox_id"])
                    with contextlib.suppress(StoreError):
                        if control_plane.STORE.release_runtime(
                            row["tenant"], row["sandbox_id"]
                        ):
                            reconciled_rows += 1
            # Direction two uses only surviving and live_rows and does not depend on the fresh snapshot - it runs as
            # usual even when the re-list above failed; orphan Pods must not be missed because direction one stopped.
            row_ids = {row["sandbox_id"] for row in live_rows}
            for sandbox_id, tenant in surviving:
                if not tenant or sandbox_id in row_ids:
                    continue
                control_plane.delete_runtime(sandbox_id)
                reconciled_orphans += 1

    # The idle verdict comes from the store, never from the volume. It used to read
    # .sandbox/last_used_at, a file inside the tenant's own writable tree owned by the
    # same uid the shell runs as: deleting it made last_used_at fall back to 0, and the
    # `and last_used_at` guard below then made the delete condition unsatisfiable, so the
    # directory stayed forever. Since the global cap counts directories on the volume,
    # one tenant could fill it and turn every other tenant's create into a 429.
    removed_workspaces = 0
    try:
        candidates = control_plane.STORE.idle_workspaces(control_plane.WORKSPACE_IDLE_TTL_SECONDS)
    except Exception as exc:
        # Without the store no Workspace is collected, but that must not interrupt Runtime collection -
        # the Runtime is the part that costs money. Log it so that "collection stopped" is visible.
        print(f"[reaper] workspace sweep skipped: {exc}", flush=True)
        candidates = []
    for workspace_id in candidates:
        if workspace_id and workspace_id not in active_workspaces:
            try:
                control_plane.volume_agent_request(
                    "DELETE",
                    f"/v1/workspaces/{workspace_id}",
                    query={"remove": "1"},
                    timeout=120,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[reaper] {workspace_id} remove failed: {exc}", flush=True)
                continue
            # Deleting the data must also write off the row, otherwise the quota leaks permanently - see forget_workspace_row.
            control_plane.forget_workspace_row(workspace_id)
            removed_workspaces += 1
    return {
        "runtimes": removed_runtimes,
        "workspaces": removed_workspaces,
        # Reprieves are reported separately: this is the only observable for "how many runtimes that would have
        # been wrongly deleted were saved by the busy probe". Always 0 means the probe never takes effect (or nobody
        # runs long tasks); always high means the TTL is too short.
        "reprieved": reprieved_runtimes,
        # The three reconciliation counters: 0 over time is healthy; persistently non-zero means the store and the
        # cluster are drifting, and it is time to find out who creates or deletes sandboxes around the Control Plane, or
        # where the rollback path leaks.
        "reconciled_rows": reconciled_rows,
        "reconciled_orphans": reconciled_orphans,
        "stale_pending": stale_pending,
    }


def reap_expired_ticket_leases(now: int | None = None) -> int:
    current = now or int(time.time())
    leases = control_plane.KUBE.list_group(
        control_plane.SYSTEM_NAMESPACE,
        "coordination.k8s.io",
        "v1",
        "leases",
        label_selector=control_plane.TICKET_LEASE_SELECTOR,
    )
    removed = 0
    for lease in leases:
        metadata = lease.get("metadata", {})
        annotations = metadata.get("annotations", {})
        try:
            expires_at = int(annotations.get("convee.io/expires-at", "0"))
        except (TypeError, ValueError):
            continue
        name = metadata.get("name")
        if name and expires_at and expires_at < current:
            control_plane.KUBE.delete_group(
                control_plane.SYSTEM_NAMESPACE,
                "coordination.k8s.io",
                "v1",
                "leases",
                name,
            )
            removed += 1
    return removed
