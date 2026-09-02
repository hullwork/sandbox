#!/usr/bin/env python3
"""Volume role: Directly read and write the Workspace directory on the mounted volume.

Responsibility: Provide creation/deletion of list/read/write and Workspace when Runtime is not available; do not touch
     Kubernetes does not run reaper and does not issue any tokens (it cannot even get SIGNING_KEY).

🔴 The same discipline as api.py: module-level names that reference control_plane must be accessed through the `control_plane.X` attribute.
   Don't `from control_plane import X`. Tests use mock.patch.object(control_plane, ...) stubbing;
   attribute access keeps those patches visible at call time."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path, PurePosixPath
import hmac
import json
import os
from urllib.parse import parse_qs, urlparse
import re
import shutil
import stat
import tempfile
import threading
import time

from . import core as control_plane
from . import tracing
from workspace_contract import WORKSPACE_LAYOUT


_WORKSPACE_ADMISSION_LOCK = threading.Lock()

#: This module is a public interface moved from core.py. The test loader places them according to this list
#: Posted to the control_plane instance (the caller follows the control_plane.X writing method), so adding/renaming must be synchronized.
__all__ = (
    "VolumeHandler",
    "WORKSPACE_LAYOUT",
    "local_admit_workspace",
    "local_create_workspace",
    "local_list_files",
    "local_list_workspaces",
    "local_purge_workspace",
    "local_read_file",
    "local_remove_workspace",
    "local_safe_path",
    "local_write_file",
    "query_int_value",
    "workspace_dir",
)


def workspace_dir(workspace_id: str) -> Path:
    """The root directory of the Workspace on the volume where the Control Plane is mounted.

    Constraints: Control Plane hangs the entire volume, and each Workspace is a directory under it - and Runtime
         Using subPath to mount /workspace points to the same data."""
    if not control_plane.WORKSPACE_VOLUME_ROOT:
        raise control_plane.WorkspaceOffline("control_plane has no workspace volume mounted")
    if not control_plane.WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("invalid workspace_id")
    return Path(control_plane.WORKSPACE_VOLUME_ROOT) / workspace_id


def local_safe_path(
    workspace_id: str,
    raw_path: str,
    *,
    allow_root: bool = False,
) -> Path:
    """Resolve a caller-supplied relative path inside this Workspace, mirroring the file-service sanitisation step by step.

    AI-LOCK: none of these checks may be dropped - null bytes, absolute paths and ``..`` are rejected, ``.sandbox``
         is reserved, and the result is **resolved and then re-verified with relative_to**. The last step exists
         for symbolic links: a string check cannot see ``link -> /etc``; only comparing the resolved paths stops it.
         Skipping "resolve, then verify" is exactly how ``../..`` traversals slip through."""
    root = workspace_dir(workspace_id).resolve(strict=False)
    if not isinstance(raw_path, str) or "\x00" in raw_path:
        raise ValueError("path must be a string")
    normalized = raw_path.strip()
    if not normalized or normalized == ".":
        if allow_root:
            return root
        raise ValueError("path must be non-empty")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path escapes workspace")
    if relative.parts and relative.parts[0] == ".sandbox":
        raise ValueError(".sandbox is reserved")
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace") from exc
    return candidate


def _relative_display(root: Path, path: Path) -> str:
    """Consistent with file-service: the root directory returns "." instead of the empty string."""
    return "." if path == root else str(path.relative_to(root))


def local_list_files(workspace_id: str, raw_path: str) -> dict:
    """Local version /v1/files/list, the returned structure is consistent with file-service field by field."""
    root = workspace_dir(workspace_id).resolve(strict=False)
    path = local_safe_path(workspace_id, raw_path, allow_root=True)
    if not path.is_dir():
        raise ValueError("path is not a directory")
    children = sorted(path.iterdir(), key=lambda item: item.name)
    entries = []
    for child in children[:control_plane.LOCAL_MAX_LIST_ENTRIES]:
        if path == root and child.name == ".sandbox":
            continue
        entries.append(
            {
                "name": child.name,
                "type": (
                    "directory"
                    if child.is_dir()
                    else "file"
                    if child.is_file()
                    else "other"
                ),
            }
        )
    return {
        "workspace_id": workspace_id,
        "path": _relative_display(root, path),
        "entries": entries,
        "truncated": len(children) > control_plane.LOCAL_MAX_LIST_ENTRIES,
    }


def _local_atomic_write(path: Path, content: bytes) -> None:
    """Temporary files in the same directory + rename overwrite, consistent with the writing method of file-service.

    You cannot directly open(path, "wb"): the code in the runtime may be reading the same file.
    Truncated writing will cause it to read half of the content, and this error only occurs during concurrency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ValueError("path is not a file")
    handle_fd, temporary = tempfile.mkstemp(prefix="sandbox-", dir=path.parent)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _record_local_activity(workspace_id: str) -> None:
    """Refresh `.sandbox/last_used_at` for a Workspace served through the volume role.

    🔴 Why: file-service's record_activity is the only other writer of this marker, and it runs inside a
       Runtime. A Workspace that is only ever read and written through the volume role - never given a
       Runtime - therefore never gets a marker, and gc_workspaces falls back to the directory's mtime,
       which writes inside subdirectories do not move. Thirty days after creation the CronJob deletes a
       Workspace that was in use that morning.
    Constraint: the marker must not decide the request. A read that succeeded is a read; failing it
         because the metadata write did not land would be the wrong trade, so OSError is swallowed.
    Constraint: never touch anything but the marker; `.sandbox` is off limits to callers (local_safe_path)
         and this is the one legitimate write into it."""
    try:
        _local_atomic_write(
            workspace_dir(workspace_id) / ".sandbox" / "last_used_at",
            f"{int(time.time())}\n".encode("ascii"),
        )
    except (OSError, ValueError):
        pass


def local_write_file(workspace_id: str, payload: dict) -> dict:
    """Local version /v1/files/write.

    Why does the volume role need to be able to write: Agent writing files to the Workspace through HTTP is the ** control plane
    Writing** does not require an execution environment - the typical process is "write the code first, then run", making writing also rely on Runtime
    It is equivalent to paying a cold start every time you write a file. The industry’s control volume API (E2B Volumes,
    Modal Volume) also provides write.

    Still not provided is glob/grep/checkpoint: those that traverse or pack the entire Workspace,
    Running on this shared replica spreads the overhead of one tenant to all tenants."""
    raw_path = payload.get("path")
    content = payload.get("content")
    if not isinstance(raw_path, str):
        raise ValueError("path must be a string")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > control_plane.LOCAL_MAX_FILE_BYTES:
        raise ValueError("file is too large")
    root = workspace_dir(workspace_id).resolve(strict=False)
    path = local_safe_path(workspace_id, raw_path)
    _local_atomic_write(path, encoded)
    _record_local_activity(workspace_id)
    return {
        "workspace_id": workspace_id,
        "path": _relative_display(root, path),
        "chars": len(content),
        "bytes": len(encoded),
    }


def local_read_file(
    workspace_id: str,
    raw_path: str,
    offset: int = 1,
    limit: int = 0,
) -> dict:
    """Local version /v1/files/read. The window semantics are exactly the same as file-service, including clipped_line."""
    root = workspace_dir(workspace_id).resolve(strict=False)
    path = local_safe_path(workspace_id, raw_path)
    if not path.is_file():
        raise ValueError("path is not a file")
    if path.stat().st_size > control_plane.LOCAL_MAX_READ_SOURCE_BYTES:
        raise ValueError("file is too large")
    start = max(offset, 1)
    window = min(limit, control_plane.LOCAL_MAX_READ_LINES) if limit > 0 else control_plane.LOCAL_MAX_READ_LINES
    lines: list[str] = []
    used = 0
    line_number = 0
    stopped_on_budget = False
    clipped_line = 0
    clipped_length = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number < start:
                    continue
                if len(lines) >= window:
                    stopped_on_budget = True
                    break
                if not lines and len(line) > control_plane.LOCAL_MAX_READ_CHARS:
                    lines.append(line[:control_plane.LOCAL_MAX_READ_CHARS])
                    used = control_plane.LOCAL_MAX_READ_CHARS
                    stopped_on_budget = True
                    clipped_line = line_number
                    clipped_length = len(line)
                    break
                if used + len(line) > control_plane.LOCAL_MAX_READ_CHARS and lines:
                    stopped_on_budget = True
                    break
                lines.append(line)
                used += len(line)
            else:
                line_number += 1  #Finished reading: points to the next line after the last line
    except UnicodeDecodeError as exc:
        raise ValueError("file is not UTF-8 text") from exc
    if not lines and line_number and start > line_number:
        raise ValueError(f"offset {start} is past end of file")
    end = start + len(lines) - 1 if lines else start - 1
    payload = {
        "workspace_id": workspace_id,
        "path": _relative_display(root, path),
        "content": "".join(lines),
        "start_line": start,
        "end_line": end,
        "truncated": stopped_on_budget,
    }
    if stopped_on_budget:
        payload["next_offset"] = end + 1
    if clipped_line:
        payload["clipped_line"] = clipped_line
        payload["clipped_length"] = clipped_length
    _record_local_activity(workspace_id)
    return payload

def local_create_workspace(workspace_id: str) -> dict:
    """Build the Workspace directory structure idempotently on the volume."""
    root = workspace_dir(workspace_id)
    created = not root.exists()
    for directory in WORKSPACE_LAYOUT:
        (root / directory).mkdir(parents=True, exist_ok=True)
    marker = root / ".sandbox" / "created_at"
    if not marker.exists():
        marker.write_text(f"{int(time.time())}\n", encoding="ascii")
    return {"workspace_id": workspace_id, "created": created}


def local_admit_workspace(workspace_id: str, maximum: int) -> dict:
    """Atomically enforce the directory quota and create one Workspace.

    Admission belongs beside the shared directory inventory. Keeping the
    check and mutation under one volume-role lock avoids both a cross-Pod
    list round trip and the control-plane-replica race of the old caller-side
    lock.
    """
    if maximum < 1:
        raise ValueError("maximum must be positive")
    with _WORKSPACE_ADMISSION_LOCK:
        root = workspace_dir(workspace_id)
        if not root.is_dir():
            # Same filter as local_list_workspaces: only directories named like a
            # Workspace count. `lost+found` or an operator's stray directory used
            # to take a slot of the quota without ever appearing in the listing.
            existing = sum(
                child.is_dir()
                and control_plane.WORKSPACE_ID.fullmatch(child.name) is not None
                for child in Path(control_plane.WORKSPACE_VOLUME_ROOT).iterdir()
            )
            if existing >= maximum:
                raise control_plane.KubeError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    f"workspace capacity reached ({existing}/{maximum})",
                )
        return local_create_workspace(workspace_id)


def _read_marker(root: Path, name: str) -> str | None:
    try:
        value = (root / ".sandbox" / name).read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def local_list_workspaces() -> dict:
    """List all Workspaces on the volume.

    This is the **only truth** about the existence of Workspace - after file-service is included in Runtime
    Workspace no longer has its own Pod, only this directory on the volume remains. last_used_at by file-service
    The record_activity is written under .sandbox, so the Runtime can still be read even if it has been retired long ago."""
    if not control_plane.WORKSPACE_VOLUME_ROOT:
        raise control_plane.WorkspaceOffline("no workspace volume mounted")
    root = Path(control_plane.WORKSPACE_VOLUME_ROOT)
    workspaces = []
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if not child.is_dir() or not control_plane.WORKSPACE_ID.fullmatch(child.name):
            continue
        workspaces.append(
            {
                "id": child.name,
                "created_at": _read_marker(child, "created_at"),
                "last_used_at": _read_marker(child, "last_used_at"),
            }
        )
    return {"workspaces": workspaces}


def local_remove_workspace(workspace_id: str) -> dict:
    """Delete the entire Workspace directory, including .sandbox.

    The difference with purge is deliberate: purge is "clear the content, the Workspace is still there" (operation and maintenance action),
    remove is "this Workspace is finished" (GC action). If combined into one, GC will leave a mark on the volume
    There are a bunch of empty shell directories with only .sandbox left, but they still appear in the list and occupy inodes."""
    root = workspace_dir(workspace_id)
    if not root.is_dir():
        raise FileNotFoundError(workspace_id)
    shutil.rmtree(root)
    return {"workspace_id": workspace_id, "removed": True}


def local_purge_workspace(workspace_id: str) -> dict:
    """Clear the Workspace contents, keeping the .sandbox.

    AI-LOCK: The semantics of purge_workspace_contents of file-service must be consistent -
         Keep .sandbox (it contains life cycle metadata, if you delete GC, you will lose the criteria), and ** does not follow
         Symbolic link** (use lstat to determine the type and unlink to delete the link itself). Following the link and deleting it is equivalent to letting
         A link in the Workspace that points outside the volume takes the data elsewhere with it."""
    root = workspace_dir(workspace_id)
    if not root.is_dir():
        raise FileNotFoundError(workspace_id)
    files = 0
    total = 0
    for child in list(root.iterdir()):
        if child.name == ".sandbox":
            continue
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            # The directory itself may contain links. By default, rmtree does not follow the links and delete the content pointed to by the links.
            shutil.rmtree(child, ignore_errors=False)
            continue
        if stat.S_ISREG(info.st_mode):
            files += 1
            total += info.st_size
        child.unlink()
    return {
        "workspace_id": workspace_id,
        "purged": True,
        "files_removed": files,
        "bytes_removed": total,
    }


class VolumeHandler(BaseHTTPRequestHandler):
    """The Control Plane agent on the Workspace volume runs in the copy that holds the entire volume.

    Responsibilities: Responsible for Workspace granular operations (listing, creating directory structure, purge content) and Runtime
         Offline access (list/read/write) when not in use. write is **control surface write** (see
         api.py's _OFFLINE_OPERATIONS argument: excluding it is equivalent to making every time you write a file pay first
         A ~16s cold start is a behavioral degradation rather than a tightening); glob/grep still only belongs to Runtime——
         That is the execution-time capability. Running a full traversal on this shared copy is equivalent to shaking a tenant's
         Proliferated to all tenants.
    Constraints: **It is a different role of the same image as Control Plane** (SANDBOX_CONTROL_PLANE_ROLE=volume).
         The reason why Control Plane is not allowed to mount the volume by itself: PVC is a namespace-level resource, and the volume is in
         sandbox-workloads and Control Plane are in sandbox-system and cannot be mounted across namespaces.
    Constraint: Authentication only confirms that the caller is Control Plane (static VOLUME_AGENT_TOKEN). Deliberately not using it
         per-workspace internal_token - that requires this process to hold SIGNING_KEY,
         And it runs in the namespace of untrusted workloads.
    AI-LOCK: Allow writing (for reasons, see _OFFLINE_OPERATIONS of api.py: writing is control plane writing,
         No cold start required); **Prohibited** edit / glob / grep / checkpoint / delete a single file.
         This role can see all Workspace content, and its attack surface must stop at the Workspace
         Granularity + Whole File Writes - One more operation makes the entire volume writable."""

    server_version = "sandbox-volume/0.1.0"

    trace_id: str = ""
    request_span: tracing.Span | None = None
    response_status: int = 500

    def handle_one_request(self) -> None:
        self.command = ""
        self.trace_id = ""
        self.request_span = None
        self.response_status = 500
        try:
            super().handle_one_request()
        finally:
            if self.request_span is not None:
                self.request_span.set_attribute(
                    "http.response.status_code", self.response_status
                )
                self.request_span.end(
                    error=RuntimeError("http error")
                    if self.response_status >= 500 else None
                )

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self.trace_id, flags, parent_span_id = tracing.inbound_context(self.headers)
            tracing.set_current(self.trace_id, flags, parent_span_id)
            self.request_span = tracing.start_span(
                "volume.http.request",
                kind=2,
                attributes={
                    "http.request.method": self.command,
                    "http.route": self.metric_route(urlparse(self.path).path),
                },
            )
        return parsed

    @classmethod
    def metric_route(cls, path: str) -> str:
        if cls._FILES_ROUTE.fullmatch(path):
            return "/v1/workspaces/{id}/files/{operation}"
        if cls._WRITE_ROUTE.fullmatch(path):
            return "/v1/workspaces/{id}/files/write"
        if cls._WORKSPACE_ROUTE.fullmatch(path):
            return "/v1/workspaces/{id}"
        if path in {"/livez", "/readyz", "/healthz"}:
            return path
        return "/unmatched"

    def current_trace_id(self) -> str:
        if not self.trace_id:
            self.trace_id = tracing.new_trace_id()
            tracing.set_current(self.trace_id)
        return self.trace_id

    def send_response(self, code, message=None):
        self.response_status = int(code)
        super().send_response(code, message)
        self.send_header(tracing.REQUEST_ID_HEADER, self.current_trace_id())

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    _FILES_ROUTE = re.compile(r"^/v1/workspaces/(ws-[a-f0-9]{12})/files/(list|read)$")
    _WRITE_ROUTE = re.compile(r"^/v1/workspaces/(ws-[a-f0-9]{12})/files/write$")
    _WORKSPACE_ROUTE = re.compile(r"^/v1/workspaces/(ws-[a-f0-9]{12})$")

    def _authorized(self) -> bool:
        # Shares the same parser as ApiHandler.bearer_token: non-ASCII headers are the same here
        # will cause compare_digest to throw a TypeError, and _handle calls it outside the try.
        token = control_plane.parse_bearer_token(self.headers.get("Authorization", ""))
        # Just answer "Are you a Control Plane?" **Not** a per-workspace token - that requires
        # SIGNING_KEY, and this role must not hold it (see the deployment configuration notes). Who may read which
        # Workspace is determined by the Control Plane before forwarding.
        return bool(control_plane.VOLUME_AGENT_TOKEN) and hmac.compare_digest(
            token, control_plane.VOLUME_AGENT_TOKEN
        )

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if length < 0 or length > control_plane.MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        if method == "GET" and parsed.path in ("/livez", "/healthz"):
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if not self._authorized():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            payload = self._route(method, parsed.path, query)
        except control_plane.KubeError as exc:
            self.send_json(exc.status, {"error": str(exc)})
            return
        except control_plane.WorkspaceOffline as exc:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except FileNotFoundError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "workspace not found"})
            return
        except (OSError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if payload is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.send_json(HTTPStatus.OK, payload)

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> dict | None:
        if method == "GET" and path == "/v1/workspaces":
            return local_list_workspaces()

        match = self._WORKSPACE_ROUTE.match(path)
        if match:
            workspace_id = match.group(1)
            if method == "POST":
                maximum = query_int_value(query, "maximum", 0)
                if maximum:
                    return local_admit_workspace(workspace_id, maximum)
                return local_create_workspace(workspace_id)
            if method == "DELETE":
                if query.get("remove", [""])[0].lower() in {"1", "true", "yes"}:
                    return local_remove_workspace(workspace_id)
                return local_purge_workspace(workspace_id)
            return None

        match = self._WRITE_ROUTE.match(path)
        if match and method == "POST":
            workspace_id = match.group(1)
            if not workspace_dir(workspace_id).is_dir():
                raise FileNotFoundError(workspace_id)
            return local_write_file(workspace_id, self._read_json())

        match = self._FILES_ROUTE.match(path)
        if match and method == "GET":
            workspace_id, operation = match.groups()
            # First, distinguish between "Workspace is gone" and "the path is written incorrectly". If it doesn’t matter, report both.
            # "path is not a directory", and Control Plane relies on this difference to decide whether to refresh the list
            # Or return the error unchanged to the caller.
            if not workspace_dir(workspace_id).is_dir():
                raise FileNotFoundError(workspace_id)
            raw_path = query.get("path", [""])[0]
            if operation == "list":
                return local_list_files(workspace_id, raw_path or ".")
            return local_read_file(
                workspace_id,
                raw_path,
                query_int_value(query, "offset", 1),
                query_int_value(query, "limit", 0),
            )
        return None

    def do_GET(self) -> None:  #noqa: N802 (Convention of BaseHTTPRequestHandler)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


def query_int_value(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query.get(key, [""])[0]
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
