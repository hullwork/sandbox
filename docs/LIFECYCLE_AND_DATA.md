# Runtime, workspace, and data lifecycle

## Resource lifecycle

1. A caller identity deterministically resolves to a workspace owner and ID.
2. Control Plane admits or reuses the workspace, then creates a Runtime from an approved
   template and waits for health.
3. Control Plane issues a short-lived scoped token; SDK/MCP traffic reaches only that
   Runtime/workspace boundary.
4. Activity refreshes the idle deadline but never the absolute Runtime deadline.
5. Release or reaping deletes the Runtime Pod. Workspace data survives until its
   separate idle policy, explicit purge, or operator storage action.

Creation records use pending/ready/error states so a failed Kubernetes operation is
reconciled instead of silently consuming capacity forever. Control Plane shutdown stops new
work, drains bounded in-flight requests, and lets reconciliation repair stale state.

## Persistence

| Data | Location | Survives Runtime deletion | Deletion authority |
| --- | --- | --- | --- |
| Workspace files | Workspace PVC/shared subpath | Yes | Workspace purge/GC or operator storage policy |
| Runtime process/memory/root layer | Runtime Pod | No | Runtime release/TTL/Pod deletion |
| Ownership, quota and audit state | PostgreSQL; SQLite only for development | Yes if durable backend is used | Admin/API retention policy |
| Checkpoints and objects | S3-compatible bucket | Yes | API delete, checkpoint retention GC, or bucket policy |

Checkpoint restore is a file-level replacement/import operation. It is not a live
process snapshot and should be tested for application consistency.

## Backup, deletion, and recovery

Production operators must back up PostgreSQL, workspace storage, object storage, and
the non-rotating `WORKSPACE_ID_KEY` as one recovery set. Document RPO/RTO for the
chosen providers. Deleting only Kubernetes objects does not prove external volumes or
object versions were erased. Privacy or compliance erasure must verify all four data
planes, including backups and versioned objects.

### Restore order

Restore the recovery set in this order so that every layer can be validated against
the previous one:

1. **Database** (PostgreSQL dump): re-creates tenants, Workspace ownership, quota,
   template, and audit rows. Nothing else can be checked until this exists.
2. **Workspace storage** (PVC snapshot or file-level backup of the shared volume):
   the `subPath` directories must match the `workspace_id` values now in the database.
3. **`WORKSPACE_ID_KEY`** (from the secret backup): Control Plane derives `workspace_id` from
   the session identity with this key. Start Control Plane with the restored key and verify
   that `POST /v1/workspaces/resolve` for a known session returns the expected
   Workspace before admitting traffic. A wrong key yields new, empty Workspaces and
   leaves the restored directories unclaimed.

Object storage can be restored in parallel with step 2, but it is checked by step 3
as well: checkpoint keys are `workspaces/<workspace_id>/checkpoints/<id>.tar.gz`,
so a wrong `WORKSPACE_ID_KEY` also makes every existing checkpoint unreachable
through the API even though the objects are still in the bucket.

The repository does not currently ship backup or restore scripts; the order above is
the contract an operator's tooling must follow.

Upgrades must preserve workspace-ID derivation and database compatibility. Roll back
application images only when their schema and stored record formats remain compatible.
