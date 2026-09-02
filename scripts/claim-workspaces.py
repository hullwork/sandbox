#!/usr/bin/env python3
"""Function entrance: Claim the existing Workspace previously built by multi-tenants to a tenant at one time.

IMPORTANT - the caller is not in this repository. Nothing here invokes this script, so a
     reference scan run inside this tree reports it as dead. It is not: the operational
     procedure that drives it, together with the credential, the port-forward and the
     quota caveats around these flags, lives in the deployment guide of the consumer
     product that owned these Workspaces before multi-tenancy. Do not delete this file on
     a "no references" finding alone - that finding is only evidence about the tree it was
     run in, and this one is driven from outside it. (The consumer is deliberately not
     named here: tests/test_standalone_contract.py forbids this repository from carrying
     a reference to a particular consumer, and that invariant is worth more than the
     convenience of one name. The pointer in the other direction is explicit.)

Background: The Workspace built before multi-tenancy has no ownership record in `sandbox_workspaces`, and
     Control Plane's `scope_workspaces` deliberately does not show "directories without ownership records" to tenants.
     (Those are either in stock or hand-stuffed into rolls). So this batch of Workspace has a
     None of the tenant keys are visible - this script adds them to the ownership table and re-owns them.

Responsibilities: Only make up for ownership records. Don’t touch files on the volume, don’t create tenants, don’t change quotas – those three things are intentional
     Management actions should not be a side effect of a migration.

Usage:
    # Both the Control Plane and the store database must be reachable: run inside the cluster directly, or port-forward from outside.
    export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
    export SANDBOX_TOKEN=<admin key or static control-plane-token>
    export SANDBOX_STORE_BACKEND=postgresql # The same set of variables as Control Plane Deployment
    export SANDBOX_DB_HOST=127.0.0.1
    export SANDBOX_DB_PASSWORD_FILE=/path/to/password

    python3 scripts/claim-workspaces.py --tenant local # only view the list
    python3 scripts/claim-workspaces.py --tenant local --apply # Real writing

Constraints: Default is dry-run; `--apply` writes to the store. Check the printed list before proceeding - once the wrong tenant
     has been admitted the script can no longer see it (every Workspace is then "owned") and only a human can fix it."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from sandbox_platform.control_plane_transport import ControlPlaneError, ControlPlaneTransport  # noqa: E402
from control_plane.store import Store, StoreError  # noqa: E402


# The claimed ownership record looks like this. The three values are all self-descriptions of the migration product, and are deliberately not pretending to be real calls.
#
# No ownership existed before multi-tenancy and none can be reconstructed now. Anyone who sees legacy/unclaimed knows at once
# that the row came from this migration rather than from a real caller.
#
# All three values must pass store.FREEFORM (1-128 characters, no control bytes), see register_workspace.
CLAIM_PRINCIPAL_KIND = "legacy"
CLAIM_PRINCIPAL_ID = "unclaimed"
# session_key is not empty and no default value is available, fill in workspace_id itself: it is at least a true value,
# And naturally it will not be confused with other rows of the same tenant.
WORKSPACE_ID = re.compile(r"^ws-[a-f0-9]{12}$")


@dataclass(frozen=True)
class Row:
    """The current ownership of a Workspace. If owner is None, it means it is to be claimed."""

    workspace_id: str
    owner: str | None


def control_plane_url() -> str:
    value = os.getenv("SANDBOX_CONTROL_PLANE_URL", "http://127.0.0.1:18080")
    return value.rstrip("/")


def control_plane_token() -> str:
    token = os.getenv("SANDBOX_TOKEN")
    if not token:
        raise SystemExit(
            "SANDBOX_TOKEN is required "
            "(use an admin key or the static control-plane-token; a tenant key cannot "
            "see the unclaimed workspaces)"
        )
    return token


def request(method: str, path: str, timeout: float = 30.0) -> dict:
    """Downstream call: sandbox-control-plane via HTTP.

    Failure handling: Connection failure/timeout are uniformly converted into ControlPlaneError(502), which has the same semantics as sandboxctl;
             Also no retries - retrying the migration script will only make it harder to judge whether it worked or not."""
    result, _ = ControlPlaneTransport(control_plane_url(), control_plane_token()).request(
        method, path, timeout=timeout
    )
    return result


def open_store() -> tuple[Store, str]:
    """Press Control Plane to open the control plane storage with the same set of environment variables, and reply "Where are you connected to?"

    AI-LOCK: The duplication of this paragraph with `_open_store` of core.py is intentional - import core.py
         It will ask for SANDBOX_CONTROL_PLANE_TOKEN / SIGNING_KEY and build KubeClient during the import stage.
         This script needs to be able to run outside the cluster when there is only one DB tunnel in hand. Change the connection there
         Parameters (variable name, default value, password file location) must be changed here.

    The returned description is printed in the plan header: this is a store write, and which environment the connection
    points at must be confirmable at a glance before execution rather than from memory of what was exported earlier."""
    backend = os.getenv("SANDBOX_STORE_BACKEND", "").strip().lower()
    if not backend:
        raise SystemExit(
            "SANDBOX_STORE_BACKEND is not configured: without control-plane "
            "storage there is no ownership table, Control Plane runs in single-tenant "
            "mode, and nothing can be claimed"
        )
    if backend == "sqlite":
        path = os.getenv("SANDBOX_STORE_PATH", "/tmp/sandbox-control-plane.db")
        return Store.sqlite(path), f"sqlite {path}"
    if backend != "postgresql":
        raise SystemExit(
            f"SANDBOX_STORE_BACKEND must be postgresql or sqlite, received {backend!r}"
        )
    password_file = os.getenv(
        "SANDBOX_DB_PASSWORD_FILE", "/var/run/sandbox-db/password"
    )
    try:
        password = pathlib.Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read database password {password_file}: {exc}") from exc
    host = os.getenv("SANDBOX_DB_HOST", "sandbox-postgres")
    port = int(os.getenv("SANDBOX_DB_PORT", "5432"))
    dbname = os.getenv("SANDBOX_DB_NAME", "sandbox")
    user = os.getenv("SANDBOX_DB_USER", "sandbox")
    store = Store.postgres(
        {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": user,
            "password": password,
            "connect_timeout": int(os.getenv("SANDBOX_DB_CONNECT_TIMEOUT", "5")),
            # To distinguish it from Control Plane: pg_stat_activity must be able to recognize that this is a migration script being written.
            "application_name": "sandbox-claim-workspaces",
        }
    )
    return store, f"postgresql {user}@{host}:{port}/{dbname}"


def assert_admin_credential() -> None:
    """Make sure you have admin credentials (and do not represent a tenant).

    🔴 Take the tenant key and run this script and you will get a **filtered** list: the tenant cannot be seen and has no ownership.
    directory, that is, all the objects to be claimed happen to be invisible. The script then said seriously "No need
    "claimed", which looks exactly the same as "already claimed" - the migration is judged to be complete.
    So explore an admin-only endpoint first and turn this situation into a hard failure instead of an empty list."""
    try:
        request("GET", "/v1/admin/tenants")
    except ControlPlaneError as exc:
        # 401 = the Control Plane does not know this key; 403 = it knows the key but it is not admin (or X-Sandbox-Tenant was sent).
        if exc.status in (401, 403):
            raise SystemExit(
                f"SANDBOX_TOKEN is not an admin credential ({exc}). "
                "A tenant key cannot see unclaimed workspaces, so this script "
                "would only receive an empty list; use an admin key or static "
                "control-plane-token"
            ) from None
        raise


def volume_workspace_ids() -> list[str]:
    """The Workspace ID that actually exists on the volume.

    Go to the Control Plane instead of reading the volume yourself: the volume (PVC) is in sandbox-workloads and has only the volume role
    It can be found that the Control Plane is its only external exit; by the way, this jump also proves that the credentials are indeed valid."""
    entries = request("GET", "/v1/workspaces").get("workspaces", [])
    ids = [str(entry.get("id") or "") for entry in entries]
    unknown = [wid for wid in ids if not WORKSPACE_ID.fullmatch(wid)]
    if unknown:
        # The volume role has been filtered according to the same rule when listed in the directory. If it does appear, it means that the upstream has changed.
        # Better to stop and let a human look than to write an ID of unknown origin into the ownership table
        # ——That table does not verify the workspace_id, and writing it in is a permanent record.
        raise SystemExit(
            "Control Plane returned entries that do not look like workspace IDs; investigate before migration: "
            + ", ".join(sorted(unknown)[:10])
        )
    return ids


def build_plan(store: Store, workspace_ids: list[str]) -> list[Row]:
    """Check the ownership one by one. Anyone who has the owner will not be touched. This is the idempotence of re-running.

    Checking one at a time (rather than pulling the entire table in memory) is because the number of Workspaces on the volume is limited.
    SANDBOX_MAX_WORKSPACES constraint (default 64), the enumeration is closer to the semantics of owner_of."""
    return [Row(wid, store.owner_of(wid)) for wid in workspace_ids]


def print_plan(rows: list[Row]) -> None:
    print(f"{'WORKSPACE':<18} {'OWNER':<16} ACTION")
    for row in rows:
        action = "claim" if row.owner is None else "skip"
        print(f"{row.workspace_id:<18} {row.owner or '-':<16} {action}")


def claim(
    store: Store, tenant_id: str, rows: list[Row]
) -> tuple[list[str], list[str]]:
    """Claim all unowned Workspaces and return (ID of successful claim, failure description).

    No existing rows are touched - this is the idempotence of reruns. register_workspace for same tenant
    User re-entry will update last_used_at, which has the semantics of "used again". A migration does not qualify for the claim.
    It, not to mention that what is pushed out by it is the criterion of GC.

    Single failure will not interrupt: a line cannot be written (typically it has just been recognized by another tenant in the concurrent session) and should not hold back the rest.
    Dozens of legal claims, and reruns would have been safe. The failure list is reported once at the end, and the exit code is non-0.

    Constraint: The summary number must be calculated from the return value here, and cannot be calculated based on the above plan - the plan is "intended to do"
         "What", only the return value here is "what was actually done"."""
    claimed: list[str] = []
    failures: list[str] = []
    for row in rows:
        if row.owner is not None:
            continue
        try:
            store.register_workspace(
                tenant_id,
                row.workspace_id,
                principal_kind=CLAIM_PRINCIPAL_KIND,
                principal_id=CLAIM_PRINCIPAL_ID,
                session_key=row.workspace_id,
            )
        except StoreError as exc:
            failures.append(f"{row.workspace_id}: {exc}")
            continue
        claimed.append(row.workspace_id)
        print(f"claimed {row.workspace_id} -> {tenant_id}")
    return claimed, failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claim-workspaces",
        description=(
            "Claim pre-multi-tenant workspaces for one tenant "
            "(prints a plan by default; --apply writes records)"
        ),
    )
    parser.add_argument(
        "--tenant",
        default="local",
        help="destination tenant (default: local); the tenant must already exist",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write ownership records; without this flag, print the plan only",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    store, target = open_store()
    try:
        tenant = store.get_tenant(args.tenant)
    except StoreError as exc:
        raise SystemExit(f"control-plane store unavailable: {exc}") from None
    if tenant is None:
        # Deliberately not automatically create tenants: Creating tenants is a deliberate management action (a quota must be set, and a key must be signed later).
        # It should not be a side effect of a migration - especially when --tenant is typed incorrectly, the automatic build will quietly create
        # Take out a tenant that no one knows about and stuff all the inventory in there.
        raise SystemExit(
            f"tenant does not exist: {args.tenant}. Create it before migration:\n"
            f"  curl -fsS -X POST {control_plane_url()}/v1/admin/tenants \\\n"
            f'    -H "Authorization: Bearer $SANDBOX_TOKEN" \\\n'
            f"    -H 'Content-Type: application/json' \\\n"
            f"    -d '{json.dumps({'id': args.tenant, 'display_name': args.tenant})}'"
        )

    assert_admin_credential()
    try:
        rows = build_plan(store, volume_workspace_ids())
        used = store.count_workspaces(tenant.id)
    except StoreError as exc:
        raise SystemExit(f"control-plane store unavailable: {exc}") from None

    claimable = [row for row in rows if row.owner is None]
    print(f"control_plane   {control_plane_url()}")
    print(f"store    {target}")
    print(
        f"tenant   {tenant.id} ({tenant.display_name}, {tenant.status}) "
        f"used {used}/{tenant.max_workspaces}"
    )
    print()
    print_plan(rows)
    print()
    if not tenant.active:
        # No blocking: The claim itself does not allow any operation to the deactivated tenant. But it still can’t be used after claiming it, please make it clear.
        print(f"warning: tenant {tenant.id} is {tenant.status}; it remains inoperable after claiming")
    if used + len(claimable) > tenant.max_workspaces:
        # Quotas are only assessed on the control_plane's creation path, and claims bypass it. Claiming excess will not fail, but then
        # This tenant can’t build a new Workspace—it’s better to say it now than to have people work backwards from 429 later.
        print(
            f"warning: {used + len(claimable)} workspaces after claiming would exceed quota "
            f"{tenant.max_workspaces}; the tenant will not be able to create more (adjust quota first)"
        )
    if not args.apply:
        print(
            f"dry-run: {len(rows)} workspaces on the volume, {len(claimable)} unclaimed, "
            f"{len(rows) - len(claimable)} already owned. Review the list and add --apply to write."
        )
        return 0

    claimed, failures = claim(store, tenant.id, rows)
    print(
        f"applied: claimed {len(claimed)}, "
        f"skipped {len(rows) - len(claimable)} already owned, failed {len(failures)}"
    )
    if failures:
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ControlPlaneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
