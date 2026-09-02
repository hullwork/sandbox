#!/usr/bin/env python3
"""Function entrance: Sandbox operation and maintenance CLI - directly connected to the control_plane to view the sandbox, view templates, and place the sandbox.

Responsibilities: Provide **operators** with an observation entrance that does not rely on kubectl; not responsible for end-user
     Multi-tenant view (that needs to be filtered by login identity, you must use the server proxy, not here).

Constraints: This tool holds Control Plane admin token and can see and release **any** sandbox. Control Plane only does
     The storage path partition does not replace Control Plane tenant authorization (see docs/SECURITY_MODEL.md), so
     This token is equivalent to the administrative rights of the sandbox cluster. **Do not connect this tool to any user-facing
     path **, nor echo its output directly to the user.

AI-LOCK: Why not reuse sandbox_client.SandboxManager - that is the agent-side client,
     With session_key, lease cache and token renewal semantics, "which sandbox is bound to the current session"
     The state is stirred in. What we need to look at from the operation and maintenance perspective is exactly the **overall**, and the two are not the same thing.

Usage:
    export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
    export SANDBOX_TOKEN=<control-plane-token>
    sandboxctl list
    sandboxctl workspaces
    sandboxctl inspect sb-abc123456789
    sandboxctl templates
    sandboxctl release sb-abc123456789

Once the sandbox is no longer a K8s Pod (self-managed runsc architecture), kubectl will not be able to see them. This tool
It will go from "convenient" to the only way to observe - then add ps/pause/resume."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

from sandbox_platform.control_plane_transport import ControlPlaneError, ControlPlaneTransport


def control_plane_url() -> str:
    return os.getenv("SANDBOX_CONTROL_PLANE_URL", "http://127.0.0.1:18080").rstrip("/")


def control_plane_token() -> str:
    token = os.getenv("SANDBOX_TOKEN")
    if not token:
        raise SystemExit(
            "SANDBOX_TOKEN is required "
            "(run `make dev-token` for the local profile)"
        )
    return token


def request_json(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict:
    """Request with an optional JSON body or query."""
    result, _ = ControlPlaneTransport(control_plane_url(), control_plane_token()).request(
        method, path, payload=payload, query=query, timeout=timeout
    )
    return result


def request(method: str, path: str, timeout: float = 15.0) -> dict:
    """Downstream call: sandbox-control-plane via HTTP.

    Failure handling: Connection failure/timeout are uniformly converted into ControlPlaneError(502), allowing the caller to get a consistent type;
             There is no retry here - retrying the operation and maintenance command will only make it more difficult to judge whether it is working or not."""
    return request_json(method, path, timeout=timeout)


def _remaining(expires_at: str | None) -> str:
    """Convert expires-at annotation to the remaining time that adults can read.

    The most common question asked by operation and maintenance is "how long will it take for this sandbox to be recycled?" The absolute timestamp requires mental calculation, which is useless."""
    if not expires_at:
        return "-"
    try:
        delta = int(expires_at) - int(time.time())
    except (TypeError, ValueError):
        return "-"
    if delta <= 0:
        return "expired"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    return f"{delta // 3600}h{(delta % 3600) // 60:02d}m"


def cmd_list(args: argparse.Namespace) -> int:
    result = request("GET", "/v1/sandboxes")
    sandboxes = result.get("sandboxes", result if isinstance(result, list) else [])
    if args.json:
        print(json.dumps(sandboxes, ensure_ascii=False, indent=2))
        return 0
    if not sandboxes:
        print("no sandboxes")
        return 0
    header = f"{'SANDBOX':<18} {'STATUS':<10} {'TEMPLATE':<14} {'WORKSPACE':<18} TTL"
    print(header)
    for item in sandboxes:
        print(
            f"{item.get('id') or '-':<18} "
            f"{item.get('status') or '-':<10} "
            f"{item.get('template') or '-':<14} "
            f"{item.get('workspace_id') or '-':<18} "
            f"{_remaining(item.get('expires_at'))}"
        )
    return 0


def cmd_workspaces(args: argparse.Namespace) -> int:
    """List Workspaces and their recycling prospects.

    Constraint: RUNTIME columns and IDLE columns must be viewed together. Workspace is only available when no Runtime is hanging.
         The idle timing is entered when
         The moment of arrival will only make one wait for a recovery that will not happen."""
    result = request("GET", "/v1/workspaces")
    workspaces = result.get("workspaces", [])
    if args.json:
        print(json.dumps(workspaces, ensure_ascii=False, indent=2))
        return 0
    if not workspaces:
        print("no workspaces")
        return 0
    print(f"{'WORKSPACE':<18} {'STATUS':<10} {'RUNTIME':<9} IDLE-GC")
    for item in workspaces:
        attached = item.get("runtime_attached")
        print(
            f"{item.get('id') or '-':<18} "
            f"{item.get('status') or '-':<10} "
            f"{'yes' if attached else 'no':<9} "
            f"{'-' if attached else _remaining(item.get('idle_expires_at'))}"
        )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    result = request("GET", f"/v1/sandboxes/{urllib.parse.quote(args.sandbox_id)}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for key in (
        "id",
        "workspace_id",
        "status",
        "template",
        "runtime_class",
        "created_at",
        "expires_at",
    ):
        if key in result:
            print(f"{key:<14} {result[key]}")
    print(f"{'ttl_left':<14} {_remaining(result.get('expires_at'))}")
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    result = request("GET", "/v1/templates")
    templates = result.get("templates", [])
    if args.json:
        print(json.dumps(templates, ensure_ascii=False, indent=2))
        return 0
    for name in templates:
        print(name)
    return 0


def _confirm_release(sandbox_id: str) -> bool:
    """Ask on the terminal; without one, insist on ``--yes``.

    The token behind this tool releases any tenant's Runtime, so a mistyped
    id is somebody else's running command gone. A script that means it says
    so with the flag; a human gets one question, on stderr so ``--json``
    output stays parseable."""
    if not sys.stdin.isatty():
        print(
            "error: release is destructive; pass --yes when stdin is not a terminal",
            file=sys.stderr,
        )
        return False
    sys.stderr.write(
        f"release runtime {sandbox_id}? its running command dies with the Pod [y/N] "
    )
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() in {"y", "yes"}:
        return True
    print("aborted", file=sys.stderr)
    return False


def cmd_release(args: argparse.Namespace) -> int:
    """Release the Runtime and keep the Workspace.

    Constraint: This is a **destructive** operation, and the running command will disappear along with the Pod. Workspace and
         Files in /workspace are not affected - this is the meaning of dual life cycle separation."""
    if not args.yes and not _confirm_release(args.sandbox_id):
        return 2
    result = request(
        "DELETE", f"/v1/sandboxes/{urllib.parse.quote(args.sandbox_id)}"
    )
    print(json.dumps(result, ensure_ascii=False) if result else "released")
    return 0


def cmd_admin_keys(args: argparse.Namespace) -> int:
    data = request("GET", "/v1/admin/keys")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for key in data.get("keys", []):
        revoked = " [revoked]" if key.get("revoked_at") else ""
        print(f"{key['id']}  {key.get('label','')}  {key.get('created_at','')}{revoked}")
    return 0


def cmd_admin_key_create(args: argparse.Namespace) -> int:
    data = request_json("POST", "/v1/admin/keys", payload={"label": args.label})
    #The plaintext only appears once (the server only stores the hash) and cannot be retrieved after printing.
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_admin_key_revoke(args: argparse.Namespace) -> int:
    request_json("DELETE", f"/v1/admin/keys/{args.key_id}")
    print(f"revoked {args.key_id}")
    return 0


def cmd_audit_tail(args: argparse.Namespace) -> int:
    data = request_json("GET", "/v1/admin/audit", query={"limit": str(args.limit)})
    for event in reversed(data.get("events", [])):
        actor = event.get("actor_id") or event.get("actor_kind") or "?"
        target = f" -> {event['target']}" if event.get("target") else ""
        print(
            f"{event.get('created_at','')} {actor} {event.get('action','')}"
            f"{target} [{event.get('outcome','')}]"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandboxctl",
        description="Sandbox Control Plane operations CLI (admin token only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list all sandboxes")
    p_list.set_defaults(func=cmd_list)

    p_ws = sub.add_parser(
        "workspaces", help="list all workspaces and idle-recovery status"
    )
    p_ws.set_defaults(func=cmd_workspaces)

    p_inspect = sub.add_parser("inspect", help="inspect one sandbox")
    p_inspect.add_argument("sandbox_id")
    p_inspect.set_defaults(func=cmd_inspect)

    p_templates = sub.add_parser("templates", help="list available runtime templates")
    p_templates.set_defaults(func=cmd_templates)

    p_release = sub.add_parser(
        "release", help="release a runtime while retaining its workspace and files"
    )
    p_release.add_argument("sandbox_id")
    p_release.add_argument(
        "--yes", action="store_true", help="release without asking (required when stdin is not a terminal)"
    )
    p_release.set_defaults(func=cmd_release)

    p_keys = sub.add_parser("admin-keys", help="list admin API keys")
    p_keys.set_defaults(func=cmd_admin_keys)
    p_key_create = sub.add_parser(
        "admin-key-create", help="issue an admin key (plaintext is shown once)"
    )
    p_key_create.add_argument("label")
    p_key_create.set_defaults(func=cmd_admin_key_create)
    p_key_revoke = sub.add_parser("admin-key-revoke", help="revoke an admin key")
    p_key_revoke.add_argument("key_id")
    p_key_revoke.set_defaults(func=cmd_admin_key_revoke)
    p_audit = sub.add_parser(
        "audit", help="print recent audit events in chronological order"
    )
    p_audit.add_argument("tail", nargs="?", default="100")
    p_audit.add_argument("--limit", dest="limit", default=None,
                         help="event count (defaults to the tail argument)")
    p_audit.set_defaults(
        func=lambda a: cmd_audit_tail(
            argparse.Namespace(limit=int(a.limit or a.tail))
        )
    )

    for p in (p_list, p_ws, p_inspect, p_templates, p_release, p_keys):
        p.add_argument("--json", action="store_true", help="output raw JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ControlPlaneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
