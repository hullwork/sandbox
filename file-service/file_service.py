#!/usr/bin/env python3
"""Canonical workspace file operations and legacy HTTP compatibility handler.

The Runtime MCP imports this module in-process.  The historical standalone
entrypoint remains only for migration and maintenance compatibility; Runtime
Pods no longer deploy a File Service sidecar.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

import capability_ticket
from workspace_contract import (
    MAX_FILE_BYTES,
    MAX_LIST_ENTRIES,
    MAX_READ_CHARS,
    MAX_READ_LINES,
    MAX_READ_SOURCE_BYTES,
)


HOST = os.getenv("FILE_SERVICE_HOST", "0.0.0.0")
PORT = int(os.getenv("FILE_SERVICE_PORT", "8081"))
# The verification key for this workspace instance, derived by Control Plane from its
# signing key and this workspace's current epoch. It is not the credential the
# caller sends: Control Plane mints a short-lived ticket signed with it per request, so
# a key read out of this container can neither be replayed elsewhere nor outlive
# the epoch it was derived under. See capability_ticket.py.
CAPABILITY_KEY = (
    os.getenv("WORKSPACE_CAPABILITY_KEY")
    or os.environ["FILE_SERVICE_CAPABILITY_KEY"]
)
CAPABILITY_EPOCH = int(os.getenv("WORKSPACE_CAPABILITY_EPOCH", "1"))
WORKSPACE_ID = os.environ["WORKSPACE_ID"]
WORKSPACE = Path(os.getenv("FILE_SERVICE_WORKSPACE", "/workspace")).resolve()
MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_BINARY_BYTES = 4 * 1024 * 1024
# read streams line by line, so a file may exceed MAX_FILE_BYTES (which caps
# whole-file writes) and still be readable one window at a time.
# Traversal budgets. Every walk is bounded on three independent axes because
# any single one can be defeated: entry count (many tiny files), byte count
# (few huge files) and wall clock (slow storage). Whichever trips first wins
# and the response is flagged truncated rather than silently short.
MAX_WALK_ENTRIES = 20_000
MAX_GREP_SOURCE_BYTES = 64 * 1024 * 1024
# 15 seconds instead of 5 seconds: File Service has been included in the Runtime Pod, so follow gVisor
# (runtimeClassName is Pod level and cannot be avoided). Actual measurement of the tree of the same 8000 files——
# Non-gVisor scans 8000/8000 in 2.89s, but gVisor only scans 4500/8000 in 5s and is cut off.
# The overall overhead is about 2.6 times (pure stat traversal is higher, up to 14 times), and it takes 15 seconds for the coverage under gVisor to return to
# About 13,000 files, comparable to previous non-gVisor levels.
#
# Think clearly before adjusting it: this is an upper limit, not a goal. Truncation itself is not a bad thing (each of the three axes has
# Reason for existence), the bad thing is that the caller cannot see it after truncation - that side is determined by agent/tools.py
# Truncate the disclosure and keep it in mind, both should be read together.
WALK_DEADLINE_SECONDS = 15.0
MAX_GLOB_RESULTS = 200
MAX_GREP_RESULTS = 200
MAX_PATTERN_CHARS = 200
MAX_CONTEXT_LINES = 5
# Directories that are pure noise in an agent workspace and cost the most to
# walk. Skipped for glob/grep only; explicit read/write paths still reach them.
PRUNED_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
        ".sandbox", ".next", ".nuxt", ".gradle", ".terraform", ".tox",
        ".cache", ".turbo", ".parcel-cache",
    }
)
# Workspace-relative paths pruned the same way. `artifacts/compressed` holds the
# full copy that density compression writes back for audit, so every large tool
# result exists there a second time: grepping a symbol would hit both the real
# file and its mirror, and the model reads that as two implementations. Matched
# by path rather than by name so a user directory that happens to be called
# `compressed` is still searchable.
PRUNED_PATHS = frozenset({"artifacts/compressed"})
MAX_CHECKPOINT_BYTES = int(
    os.getenv("MAX_CHECKPOINT_BYTES", str(64 * 1024 * 1024))
)
MAX_CHECKPOINT_SOURCE_BYTES = int(
    os.getenv("MAX_CHECKPOINT_SOURCE_BYTES", str(256 * 1024 * 1024))
)
MAX_CHECKPOINT_ENTRIES = int(
    os.getenv("MAX_CHECKPOINT_ENTRIES", "20000")
)
# Delivery bundles intentionally have their own, tighter entry budget. They are
# user-facing artifacts, not recovery images, and a huge small-file tree should
# fail closed instead of turning one post-run sync into an unbounded tar walk.
MAX_BUNDLE_ENTRIES = int(os.getenv("MAX_BUNDLE_ENTRIES", "5000"))
COPY_CHUNK_BYTES = 1024 * 1024
# Restore the swap log. The file name is fixed because the person reading it is the "next startup" when the process
# There is no status anymore.
RESTORE_JOURNAL_NAME = "restore-journal.json"
# Socket timeout for each connection. See the description at ApiHandler.timeout.
REQUEST_TIMEOUT_SECONDS = 30.0
# Query string in the log. `\S*` wraps around spaces, so '"GET /x?p=y HTTP/1.1" 200 -'
# Only discard the ?p=y section, and keep all the methods/paths/status codes.
QUERY_STRING = re.compile(r"\?\S*")


def safe_path(raw_path: str, *, allow_root: bool = False) -> Path:
    if not isinstance(raw_path, str) or "\x00" in raw_path:
        raise ValueError("path must be a string")
    normalized = raw_path.strip()
    if not normalized:
        if allow_root:
            return WORKSPACE
        raise ValueError("path must be non-empty")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path escapes /workspace")
    if relative.parts and relative.parts[0] == ".sandbox":
        raise ValueError(".sandbox is reserved")
    candidate = (WORKSPACE / Path(*relative.parts)).resolve(strict=False)
    try:
        inside = candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise ValueError("path escapes /workspace") from exc
    # The retention criterion must fall at the true position **after** resolve(). The first paragraph of the above article is judged literally,
    # In the sandbox, `ln -s .sandbox link` can bypass: the first paragraph of "link/last_used_at"
    # It is a link. After parsing, it falls into .sandbox and is still in the workspace. ⇒ Release.
    # The harm goes beyond "the sandbox can write its own volume": this check also guards the Control Plane's
    # write_file tool and restore's staging/retired temporary directories - one write to
    # .sandbox/restore-* can tamper with the snapshot being restored.
    # Both checks are retained because they prevent the same thing: the literal one blocks "direct roll call"
    # .sandbox" (even if it itself is replaced by a symbolic link pointing elsewhere), this block "wrap around the name
    # Go to the real .sandbox".
    if inside.parts and inside.parts[0] == ".sandbox":
        raise ValueError(".sandbox is reserved")
    return candidate


def query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query.get(key, [""])[0].strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def query_flag(query: dict[str, list[str]], key: str) -> bool:
    return query.get(key, [""])[0].strip().lower() in ("1", "true", "yes")


class WalkBudget:
    """Shared traversal budget for glob/grep.

    Holds the three limits in one place so a walk cannot accidentally enforce
    only some of them, and records which axis tripped for the response.
    """

    def __init__(self, *, max_entries: int = MAX_WALK_ENTRIES) -> None:
        self.max_entries = max_entries
        self.entries = 0
        self.scanned_bytes = 0
        self.deadline = time.monotonic() + WALK_DEADLINE_SECONDS
        self.truncated = False
        self.reason = ""

    def trip(self, reason: str) -> bool:
        self.truncated = True
        if not self.reason:
            self.reason = reason
        return True

    def exhausted(self) -> bool:
        if self.entries >= self.max_entries:
            return self.trip("entry limit")
        if time.monotonic() > self.deadline:
            return self.trip("time limit")
        return False


def contained_path(path: Path) -> Path | None:
    """Resolve a walked path and confirm it is still inside the workspace.

    Symlinks are the reason this exists: a Runtime Pod shares the same volume
    and can `ln -s /etc/passwd /workspace/x`. resolve() follows the link, so a
    escaping target fails relative_to() and is dropped from results.
    """
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(WORKSPACE)
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def _is_pruned(current: str, name: str) -> bool:
    # Only prune according to the list, no longer skip all directories starting with `.`.
    #
    # Pruning here is a cost optimization, not a safety boundary: it only works on glob/grep, read/write
    # You can still get in with an explicit path (see the comment above PRUNED_DIRECTORIES). So "hide"
    # There is no security change at all, but the cost is `.github/workflows/ci.yml`, `.env.example`,
    # Files such as `.claude/` that you really want to search will never be found - and the model sees "empty results".
    # It will conclude based on this that the file does not exist, and then create a new one and overwrite it.
    if name in PRUNED_DIRECTORIES:
        return True
    try:
        return relative_name(Path(current) / name) in PRUNED_PATHS
    except ValueError:
        # walk root outside the workspace: name-level pruning still applies,
        # but a workspace-relative path cannot be formed, so skip that check.
        return False


def walk_files(root: Path, budget: WalkBudget):
    """Yield regular files under root, pruning noise directories.

    os.walk with followlinks=False keeps the traversal itself from looping
    through symlinked directories; contained_path then rejects symlinked files
    whose target escapes the workspace.
    """
    for current, directories, filenames in os.walk(root, followlinks=False):
        if budget.exhausted():
            return
        directories[:] = [
            name for name in sorted(directories)
            if not _is_pruned(current, name)
        ]
        for name in sorted(filenames):
            if budget.exhausted():
                return
            budget.entries += 1
            candidate = contained_path(Path(current) / name)
            if candidate is None or not candidate.is_file():
                continue
            yield candidate


def relative_name(path: Path) -> str:
    return str(path.relative_to(WORKSPACE))


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob with real path semantics.

    fnmatch is unusable here: its `*` also matches `/`, so `src/*.py` would
    match `src/a/b/c.py`. Python 3.12 has no PurePath.full_match, so the
    translation is explicit — `**` crosses directories, `*` and `?` do not.
    A pattern with no separator is widened to any depth, since an agent that
    knows the filename rarely knows where it lives.
    """
    if not pattern:
        raise ValueError("pattern must be non-empty")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern exceeds {MAX_PATTERN_CHARS} characters")
    if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
        raise ValueError("pattern escapes /workspace")
    if "/" not in pattern:
        pattern = f"**/{pattern}"
    out = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile(f"(?s:{''.join(out)})\\Z")


def compile_pattern(raw: str, *, case_insensitive: bool) -> re.Pattern[str]:
    """Compile a search pattern, bounded in length.

    Python's re has no timeout, so a catastrophic-backtracking pattern can pin
    one CPU. The blast radius is the caller's own single-tenant Pod (200m CPU
    limit, liveness probe restarts it), and the length cap keeps the obvious
    nested-quantifier constructions out. Callers wanting literal semantics
    should pass regex=0 so this is never reached.
    """
    if not raw:
        raise ValueError("pattern must be non-empty")
    if len(raw) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern exceeds {MAX_PATTERN_CHARS} characters")
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.compile(raw, flags)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def _atomic_replace(path: Path, produce) -> None:
    """Write via a temporary file in the same directory, then rename over.

    ``produce`` receives the open binary handle; callers either hand over a
    whole bytes object or copy from a stream, so a big file never has to exist
    in memory just because the destination is written atomically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ValueError("path is not a file")
    fd, temporary = tempfile.mkstemp(prefix="sandbox-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            produce(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write(path: Path, content: bytes) -> None:
    _atomic_replace(path, lambda handle: handle.write(content))


def atomic_copy(path: Path, source) -> None:
    """Atomically write a file from a readable stream, one chunk at a time."""
    _atomic_replace(
        path,
        lambda handle: shutil.copyfileobj(source, handle, COPY_CHUNK_BYTES),
    )


# The reason for the latest activity accounting failure; None means it is currently normal. healthz bring it out because this
# When something goes wrong there's no where else to report the error - it's doing the best it can now.
_ACTIVITY_FAILURE: str | None = None


def record_activity(now: int | None = None) -> None:
    """Core function: Write the active time on the volume so that the GC can still determine whether it is idle after the Pod is gone.

    Responsibilities: Only responsible for trying to make a note; **not responsible** for deciding whether the request can be continued.
    Constraint: Never throw exceptions to the caller. It hangs on the authentication path, and `.sandbox` is in the tenant
         In a writable directory - in the sandbox, just `touch /workspace/.sandbox` will cause mkdir to throw
         FileExistsError, it will escape from the authentication and cannot even send a response. The entire file API
         100% disconnection, and /healthz is still 200. The correct sign of bad accounting is failure to keep accounts.
         It's not that the service is unavailable."""
    global _ACTIVITY_FAILURE
    try:
        metadata = WORKSPACE / ".sandbox"
        metadata.mkdir(parents=True, exist_ok=True)
        atomic_write(
            metadata / "last_used_at",
            f"{now or int(time.time())}\n".encode("ascii"),
        )
    except OSError as exc:
        # No logging: This is hung on every request. Refreshing the screen will drown out real errors. Status goes healthz.
        _ACTIVITY_FAILURE = f"{type(exc).__name__}: {exc}"
        return
    _ACTIVITY_FAILURE = None


def _restore_journal_path() -> Path:
    return WORKSPACE / ".sandbox" / RESTORE_JOURNAL_NAME


def _write_restore_journal(phase: str, staging: Path, retired: Path) -> None:
    """Record the exchange step for the next start to finish.

    The disk must be placed before moving the workspace: SIGKILL cannot reach any except, the only one still alive
    The clue is to get involved in this log. The name is stored relative to a section of .sandbox, because WORKSPACE
    The absolute path may not be the same in other Pods."""
    atomic_write(
        _restore_journal_path(),
        json.dumps(
            {"phase": phase, "staging": staging.name, "retired": retired.name},
            sort_keys=True,
        ).encode("utf-8"),
    )


def _clear_restore_journal() -> None:
    _restore_journal_path().unlink(missing_ok=True)


def _restore_journal_directories(record: object) -> tuple[str, str, str] | None:
    """Verify the log content and return (phase, staging directory name, retired directory name).

    The directory name must be a **single segment** name under .sandbox. The log files themselves live on the tenant volume, only
    If the read is not verified, a log with "../.." will allow self-healing to move the rmtree elsewhere."""
    if not isinstance(record, dict):
        return None
    phase = record.get("phase")
    staging = record.get("staging")
    retired = record.get("retired")
    if phase not in ("retiring", "installing"):
        return None
    for name in (staging, retired):
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            return None
    return phase, staging, retired  # type: ignore[return-value]


def recover_interrupted_restore() -> dict[str, object] | None:
    """At startup, end the last restore interrupted by a forced kill.

    Why is it needed: The rollback of `_swap_in_restored_tree` only hangs on `except BaseException`
    on, and SIGKILL can't go there at all. Overlay a known fact - Runtime's /activity
    Only PTY sessions are counted, so "running a 60-second restore" is a complete probation criterion for Control Plane.
    Invisible, use gracePeriodSeconds: 0 to delete the Pod when the TTL reaches the point. Putting the two things together, the user’s
    The workspace will end up with half old and half new, data will stay in .sandbox/old-*, and there won't be any automatic recovery
    path. What is added here is that path.
    (The statistical caliber of /activity is on the sandbox/runtime side and is not changed in this service; this
     Functions are the backbone of "it can heal itself even if it doesn't move", not a substitute. )

    How do the two phases end:
    - retiring: None of the new trees have been loaded into the workspace yet ⇒ Move the old entries in retired back,
      The whole old tree is restored and the staging is discarded.
    - installing: The old tree has been moved out of the workspace as a whole, and the new tree is half installed inside ⇒ Go forward
      Finishing. Choose forward rather than backward because going back requires two more rounds of rename (first replace the installed new entries
      Pull it out and put the old tree back again), each round is a new failure point; and "put what the user wants this time
      "Restore is done" is originally the caller's intention.

    Boundary: **Only process the exchange with log**. Orphaned restore-*/old-* directories without logs
    Never move - `_swap_in_restored_tree` is **deliberately** changing old-* when the rollback also fails.
    What is left is the only remaining copy of the user data. If you clear it here, it will be gone."""
    journal = _restore_journal_path()
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    metadata = WORKSPACE / ".sandbox"
    names = _restore_journal_directories(record)
    if names is None:
        # The log itself is broken - don't use it to move user data. Delete it and make it clean next time.
        journal.unlink(missing_ok=True)
        return None
    phase, staging, retired = names[0], metadata / names[1], metadata / names[2]
    if not retired.is_dir():
        # The exchange was actually successful, but the log was not deleted in time (retired was deleted on the successful path).
        journal.unlink(missing_ok=True)
        return None

    moved = 0
    if phase == "retiring":
        # The remaining items in the workspace are old items that have not been moved, and the items in retired are those that have been moved.
        # The same name will only be on one side and will not cover each other if moved back.
        for child in sorted(retired.iterdir()):
            os.replace(child, WORKSPACE / child.name)
            moved += 1
        outcome = "rolled_back"
    else:
        for child in sorted(staging.iterdir()):
            os.replace(child, WORKSPACE / child.name)
            moved += 1
        outcome = "rolled_forward"
    shutil.rmtree(staging, ignore_errors=True)
    # Retired is only deleted in this step: the old data here has either been returned to the workspace (rolled_back),
    # Either it has been replaced by a complete new tree (rolled_forward) and is no longer the only copy.
    shutil.rmtree(retired, ignore_errors=True)
    journal.unlink(missing_ok=True)
    return {"restore_recovery": outcome, "entries": moved}


def purge_workspace_contents() -> dict[str, int]:
    """Remove tenant content without traversing links outside the mount.

    ``.sandbox`` holds lifecycle metadata and restore staging, so it remains.
    The File Service mounts only one PVC subPath; this operation cannot name or
    reach another workspace.
    """
    files = 0
    total = 0
    for child in list(WORKSPACE.iterdir()):
        if child.name == ".sandbox":
            continue
        info = child.lstat()
        if stat.S_ISREG(info.st_mode):
            files += 1
            total += info.st_size
            child.unlink()
            continue
        if stat.S_ISLNK(info.st_mode):
            files += 1
            child.unlink()
            continue
        if stat.S_ISDIR(info.st_mode):
            for root, directories, names in os.walk(child, followlinks=False):
                root_path = Path(root)
                directories[:] = [
                    name for name in directories
                    if not (root_path / name).is_symlink()
                ]
                for name in names:
                    path = root_path / name
                    entry = path.lstat()
                    files += 1
                    if stat.S_ISREG(entry.st_mode):
                        total += entry.st_size
            shutil.rmtree(child)
            continue
        child.unlink()
    return {"files": files, "bytes": total}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "workspace-file/0.3"
    # Socketserver uses this value to settimeout each connection. None = None = rfile.read()
    # Wait forever: the sandbox itself can connect to 127.0.0.1:8081 and then not send a single byte (same as netns
    # The loopback does not go through NetworkPolicy), one connection pegs one thread, and dozens of them will
    # ThreadingHTTPServer crashed, and /healthz was still 200 in the meantime.
    # This is the upper limit of a single recv/send, not the upper limit of the entire request - checkpoint up and down
    # The maximum size is 64MB over loopback. If a single IO stalls for 30 seconds, the peer has most likely stopped sending.
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, fmt: str, *args: object) -> None:
        # The entire query string is lost: the request line contains ?path= / ?pattern= - the user's file path
        # and search terms will be collected by the log forwarding link on the node. The positioning information required for troubleshooting lies in methods,
        # The path and status code are enough.
        # The end is in log_message instead of log_request, because log_error uses the same method.
        # Here (the malformed request will print out the original request line).
        print(f"{self.address_string()} - {QUERY_STRING.sub('?<redacted>', fmt % args)}", flush=True)

    def send_json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_bytes(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-SHA256", hashlib.sha256(content).hexdigest())
        self.end_headers()
        self.wfile.write(content)

    def read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def require_auth(self) -> bool:
        # 🔴 The ticket must name this workspace and this epoch. Checking the
        # signature alone would let a ticket minted for another kind or another
        # subject through whenever two instances happened to share a key.
        header = self.headers.get("Authorization", "")
        ticket = header[7:] if header.startswith("Bearer ") else ""
        if capability_ticket.verify(
            CAPABILITY_KEY, ticket, "workspace", WORKSPACE_ID, CAPABILITY_EPOCH
        ):
            record_activity()
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            # Accounting is broken and still returns 200: It does not affect the file API, and restarting cannot fix it - `.sandbox`
            # Occupied is the status on the volume. If you change the Pod, it will still be there. Reporting degraded is to let someone know about this matter.
            # You can see that it is not for kubelet to restart.
            payload = {"status": "ok", "workspace_id": WORKSPACE_ID}
            if _ACTIVITY_FAILURE is not None:
                payload["activity_recording"] = "degraded"
                payload["activity_error"] = _ACTIVITY_FAILURE
            self.send_json(HTTPStatus.OK, payload)
            return
        if not self.require_auth():
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            raw_path = query.get("path", [""])[0]
            if parsed.path == "/v1/files/read":
                self.handle_read(
                    raw_path,
                    query_int(query, "offset", 1),
                    query_int(query, "limit", 0),
                )
            elif parsed.path == "/v1/files/read-binary":
                self.handle_read_binary(raw_path)
            elif parsed.path == "/v1/files/list":
                self.handle_list(raw_path)
            elif parsed.path == "/v1/files/glob":
                self.handle_glob(
                    raw_path,
                    query.get("pattern", [""])[0],
                    query_int(query, "limit", 0),
                )
            elif parsed.path == "/v1/files/grep":
                self.handle_grep(
                    raw_path,
                    query.get("pattern", [""])[0],
                    query.get("glob", [""])[0],
                    query.get("mode", ["files_with_matches"])[0],
                    query_flag(query, "case_insensitive"),
                    query_int(query, "context", 0),
                    query_flag(query, "regex"),
                    query_int(query, "limit", 0),
                )
            elif parsed.path == "/v1/files/checkpoint":
                self.handle_checkpoint()
            elif parsed.path == "/v1/files/archive":
                self.handle_archive(raw_path)
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (OSError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/v1/files/write":
                self.handle_write(payload)
            elif parsed.path == "/v1/files/write-binary":
                self.handle_write_binary(payload)
            elif parsed.path == "/v1/files/edit":
                self.handle_edit(payload)
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (OSError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:
        if not self.require_auth():
            return
        if urlparse(self.path).path != "/v1/files/checkpoint":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            length = int(raw_length)
            if length < 1 or length > MAX_CHECKPOINT_BYTES:
                raise ValueError("checkpoint archive is too large")
            # spool to disk and then handed over to restore: rfile.read(n) is a fully loaded, several hundred MB archive
            # Will push the container memory to the limit. /tmp is file-tmp emptyDir(512Mi, disk medium,
            # Capacity > MAX_CHECKPOINT_SOURCE_BYTES 256MB), packaged with checkpoint
            # TemporaryFile same path. The digest is calculated synchronously on the copy stream and does not require a second pass.
            archive_file = tempfile.TemporaryFile()
            try:
                digest = hashlib.sha256()
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ValueError("checkpoint archive ended early")
                    archive_file.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                expected = self.headers.get("X-Content-SHA256")
                if expected and not hmac.compare_digest(expected, digest.hexdigest()):
                    raise ValueError("checkpoint sha256 does not match content")
                archive_file.seek(0)
                restored = self.restore_checkpoint(archive_file)
            finally:
                archive_file.close()
            self.send_json(
                HTTPStatus.OK,
                {
                    "workspace_id": WORKSPACE_ID,
                    "sha256": digest.hexdigest(),
                    **restored,
                },
            )
        except (OSError, tarfile.TarError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self.require_auth():
            return
        if urlparse(self.path).path != "/v1/files":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = purge_workspace_contents()
            record_activity()
            self.send_json(
                HTTPStatus.OK,
                {"workspace_id": WORKSPACE_ID, "purged": True, **result},
            )
        except OSError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def handle_read(self, raw_path: str, offset: int, limit: int) -> None:
        """Read a line window.

        Streams instead of slurping so a window can be served from a file
        larger than MAX_FILE_BYTES, and so `offset` addresses the real file
        rather than a pre-truncated prefix. next_offset is the contract that
        makes paging work: it is only present when more lines remain.
        """
        path = safe_path(raw_path)
        if not path.is_file():
            raise ValueError("path is not a file")
        if path.stat().st_size > MAX_READ_SOURCE_BYTES:
            raise ValueError("file is too large")
        start = max(offset, 1)
        window = min(limit, MAX_READ_LINES) if limit > 0 else MAX_READ_LINES
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
                    # A single line over budget (minified JSON, one-line log)
                    # is hard-cut instead of dropped, so the window still
                    # carries something. next_offset then points at the NEXT
                    # line, so the clipped remainder is unreachable by paging —
                    # clipped_line exists to say so out loud rather than let
                    # the caller believe it saw the whole line.
                    if not lines and len(line) > MAX_READ_CHARS:
                        lines.append(line[:MAX_READ_CHARS])
                        used = MAX_READ_CHARS
                        stopped_on_budget = True
                        clipped_line = line_number
                        clipped_length = len(line)
                        break
                    if used + len(line) > MAX_READ_CHARS and lines:
                        stopped_on_budget = True
                        break
                    lines.append(line)
                    used += len(line)
                else:
                    line_number += 1  # loop finished: one past the last line
        except UnicodeDecodeError as exc:
            raise ValueError("file is not UTF-8 text") from exc
        if not lines and line_number and start > line_number:
            raise ValueError(f"offset {start} is past end of file")
        end = start + len(lines) - 1 if lines else start - 1
        payload = {
            "workspace_id": WORKSPACE_ID,
            "path": relative_name(path),
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
        self.send_json(HTTPStatus.OK, payload)

    def handle_glob(self, raw_path: str, pattern: str, limit: int) -> None:
        """Match files by shell glob, newest first.

        Ordering is mtime-descending on purpose: in an agent workspace the
        file touched most recently is almost always the one the caller means,
        so the head of a truncated list stays the useful part.
        """
        matcher = glob_to_regex(pattern)
        root = safe_path(raw_path, allow_root=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        budget = WalkBudget()
        found: list[tuple[float, str]] = []
        for candidate in walk_files(root, budget):
            name = relative_name(candidate)
            # Match relative to the search root so `*.py` under path=src
            # means "in src", not "anywhere with a src prefix".
            probe = name if root == WORKSPACE else str(candidate.relative_to(root))
            if not matcher.match(probe):
                continue
            try:
                found.append((candidate.stat().st_mtime, name))
            except OSError:
                continue
        found.sort(key=lambda item: (-item[0], item[1]))
        capped = min(limit, MAX_GLOB_RESULTS) if limit > 0 else MAX_GLOB_RESULTS
        truncated = budget.truncated or len(found) > capped
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "pattern": pattern,
                "matches": [name for _, name in found[:capped]],
                "total": len(found),
                "scanned": budget.entries,
                "truncated": truncated,
                "truncated_reason": budget.reason,
            },
        )

    def handle_grep(
        self,
        raw_path: str,
        pattern: str,
        file_glob: str,
        mode: str,
        case_insensitive: bool,
        context: int,
        use_regex: bool,
        limit: int,
    ) -> None:
        """Search file contents.

        Three output modes mirror ripgrep's useful subset: files_with_matches
        (cheapest, the default), count, and content. Non-UTF-8 files are
        skipped rather than failing the whole search — a workspace routinely
        holds images next to source.
        """
        if mode not in ("files_with_matches", "content", "count"):
            raise ValueError(
                "mode must be files_with_matches, content or count"
            )
        root = safe_path(raw_path, allow_root=True)
        if not root.is_dir():
            raise ValueError("path is not a directory")
        file_matcher = glob_to_regex(file_glob) if file_glob else None
        context = max(0, min(context, MAX_CONTEXT_LINES))
        if use_regex:
            regex = compile_pattern(pattern, case_insensitive=case_insensitive)
            probe = regex.search
        else:
            if not pattern:
                raise ValueError("pattern must be non-empty")
            if len(pattern) > MAX_PATTERN_CHARS:
                raise ValueError(
                    f"pattern exceeds {MAX_PATTERN_CHARS} characters"
                )
            needle = pattern.lower() if case_insensitive else pattern
            def probe(line: str) -> bool:
                return needle in (line.lower() if case_insensitive else line)
        capped = min(limit, MAX_GREP_RESULTS) if limit > 0 else MAX_GREP_RESULTS
        budget = WalkBudget()
        files: list[dict] = []
        matched_lines = 0
        for candidate in walk_files(root, budget):
            name = relative_name(candidate)
            scoped = name if root == WORKSPACE else str(candidate.relative_to(root))
            if file_matcher is not None and not file_matcher.match(scoped):
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            if budget.scanned_bytes + size > MAX_GREP_SOURCE_BYTES:
                budget.trip("byte limit")
                break
            budget.scanned_bytes += size
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            hits = [i for i, line in enumerate(lines) if probe(line)]
            if not hits:
                continue
            entry: dict = {"path": name, "count": len(hits)}
            if mode == "content":
                entry["lines"] = self.render_hits(lines, hits, context)
            files.append(entry)
            matched_lines += len(hits)
            if len(files) >= capped:
                budget.trip("result limit")
                break
        payload = {
            "workspace_id": WORKSPACE_ID,
            "pattern": pattern,
            "mode": mode,
            "files": (
                [entry["path"] for entry in files]
                if mode == "files_with_matches"
                else files
            ),
            "file_count": len(files),
            "match_count": matched_lines,
            "scanned": budget.entries,
            "truncated": budget.truncated,
            "truncated_reason": budget.reason,
        }
        self.send_json(HTTPStatus.OK, payload)

    @staticmethod
    def render_hits(lines: list[str], hits: list[int], context: int) -> list[dict]:
        """Expand hit line numbers into 1-indexed records with context.

        Overlapping context windows are merged so a dense match region is not
        emitted several times over.
        """
        wanted: set[int] = set()
        for index in hits:
            wanted.update(
                range(max(0, index - context), min(len(lines), index + context + 1))
            )
        hit_set = set(hits)
        rendered = []
        for index in sorted(wanted):
            text = lines[index]
            rendered.append(
                {
                    "line": index + 1,
                    "text": text[:500],
                    "match": index in hit_set,
                }
            )
        return rendered

    def handle_list(self, raw_path: str) -> None:
        path = safe_path(raw_path, allow_root=True)
        if not path.is_dir():
            raise ValueError("path is not a directory")
        children = sorted(path.iterdir(), key=lambda item: item.name)
        entries = []
        for child in children[:MAX_LIST_ENTRIES]:
            if path == WORKSPACE and child.name == ".sandbox":
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
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "path": (
                    "."
                    if path == WORKSPACE
                    else str(path.relative_to(WORKSPACE))
                ),
                "entries": entries,
                "truncated": len(children) > MAX_LIST_ENTRIES,
            },
        )

    def handle_read_binary(self, raw_path: str) -> None:
        path = safe_path(raw_path)
        if not path.is_file():
            raise ValueError("path is not a file")
        # Look at the size first and then read, which is the same discipline as handle_read: conversely, the Runtime is in the same
        # The entire 600MB dd out of PVC subPath will be read into the 512Mi container → OOMKilled,
        # And this path is meant to reject it.
        if path.stat().st_size > MAX_BINARY_BYTES:
            raise ValueError("file is too large")
        data = path.read_bytes()
        if len(data) > MAX_BINARY_BYTES:
            # The file can still grow after stat (the Runtime is writing on the same PVC), this is a race condition.
            raise ValueError("file is too large")
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "path": str(path.relative_to(WORKSPACE)),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        )

    def handle_write(self, payload: dict) -> None:
        raw_path = payload.get("path")
        content = payload.get("content")
        if not isinstance(raw_path, str):
            raise ValueError("path must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError("file is too large")
        path = safe_path(raw_path)
        atomic_write(path, encoded)
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "path": str(path.relative_to(WORKSPACE)),
                "chars": len(content),
                "bytes": len(encoded),
            },
        )

    def handle_edit(self, payload: dict) -> None:
        raw_path = payload.get("path")
        old = payload.get("old")
        new = payload.get("new")
        if not all(isinstance(value, str) for value in (raw_path, old, new)):
            raise ValueError("path, old and new must be strings")
        path = safe_path(raw_path)
        if not path.is_file():
            raise ValueError("path is not a file")
        content = path.read_text(encoding="utf-8")
        matches = content.count(old)
        if matches == 0:
            raise ValueError("old string was not found")
        if matches > 1:
            raise ValueError(f"old string appears {matches} times")
        updated = content.replace(old, new)
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError("file is too large")
        atomic_write(path, encoded)
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "path": str(path.relative_to(WORKSPACE)),
                "replacements": 1,
            },
        )

    def handle_write_binary(self, payload: dict) -> None:
        raw_path = payload.get("path")
        encoded = payload.get("content_base64")
        if not isinstance(raw_path, str):
            raise ValueError("path must be a string")
        if not isinstance(encoded, str):
            raise ValueError("content_base64 must be a string")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("content_base64 is invalid") from exc
        if len(data) > MAX_BINARY_BYTES:
            raise ValueError("file is too large")
        digest = hashlib.sha256(data).hexdigest()
        expected_digest = payload.get("sha256")
        if expected_digest is not None and not hmac.compare_digest(
            str(expected_digest), digest
        ):
            raise ValueError("sha256 does not match content")
        path = safe_path(raw_path)
        atomic_write(path, data)
        self.send_json(
            HTTPStatus.OK,
            {
                "workspace_id": WORKSPACE_ID,
                "path": str(path.relative_to(WORKSPACE)),
                "bytes": len(data),
                "sha256": digest,
            },
        )

    def handle_checkpoint(self) -> None:
        """Package the entire Workspace and stream it back.

        Archive into temporary files instead of BytesIO: source limit MAX_CHECKPOINT_SOURCE_BYTES default
        256MB, while this container is only 512Mi. The peak value of the BytesIO version is "compression result + getvalue()
        "Copy", a Workspace installed with incompressible content such as node_modules is enough to
        Pod reaches OOMKilled - OOMKilled only shows that the connection is reset on site, and it cannot be seen that it is packaged.
        This step is a blast. The temporary file goes to /tmp (it has been created in the Dockerfile and chown to
        65532), the price is one more sequential read, in exchange for resident memory independent of the Workspace size."""
        total = 0
        # TemporaryFile has no name and is recycled when closed; abnormal paths will not leave garbage in /tmp.
        with tempfile.TemporaryFile() as buffer:
            with tarfile.open(fileobj=buffer, mode="w:gz", dereference=False) as archive:
                for root, directories, files in os.walk(WORKSPACE, followlinks=False):
                    root_path = Path(root)
                    directories[:] = [
                        name for name in sorted(directories)
                        if not (root_path / name).is_symlink()
                        and not (root_path == WORKSPACE and name == ".sandbox")
                    ]
                    for name in sorted(files):
                        path = root_path / name
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        total += info.st_size
                        if total > MAX_CHECKPOINT_SOURCE_BYTES:
                            raise ValueError("workspace exceeds checkpoint source limit")
                        archive.add(
                            path,
                            arcname=str(path.relative_to(WORKSPACE)),
                            recursive=False,
                        )
            size = buffer.tell()
            if size > MAX_CHECKPOINT_BYTES:
                raise ValueError("compressed checkpoint exceeds size limit")
            # The summary is calculated first and then sent: X-Content-SHA256 is the response header and must be earlier than the body.
            # Therefore, there must be two times here: "read it once to calculate the abstract, and read it again to send the text."
            buffer.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: buffer.read(COPY_CHUNK_BYTES), b""):
                digest.update(chunk)
            buffer.seek(0)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-SHA256", digest.hexdigest())
            self.end_headers()
            shutil.copyfileobj(buffer, self.wfile, COPY_CHUNK_BYTES)

    def handle_archive(self, raw_path: str) -> None:
        """Stream a bounded, portable bundle for one Workspace directory.

        The archive contains regular files only and a machine-readable manifest.
        Symlinks, internal agent state, and legacy compaction transcripts are
        deliberately omitted so a durable user artifact cannot become a path
        escape or an accidental prompt-history export.
        """
        root = safe_path(raw_path)
        if not root.is_dir():
            raise ValueError("archive path is not a directory")
        candidates: list[tuple[Path, str]] = []
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name for name in sorted(directories)
                if not (current_path / name).is_symlink()
                and name not in {".agent-state", ".sandbox"}
                and not (
                    root.name == "artifacts"
                    and current_path == root
                    and name == "compaction"
                )
            ]
            for name in sorted(files):
                path = current_path / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                if len(candidates) >= MAX_BUNDLE_ENTRIES:
                    raise ValueError("archive exceeds entry limit")
                candidates.append((path, path.relative_to(root).as_posix()))
        if not candidates:
            raise ValueError("archive directory has no regular files")
        total = 0
        entries: list[tuple[str, int, str]] = []
        with tempfile.TemporaryFile() as buffer:
            with tarfile.open(fileobj=buffer, mode="w:gz", dereference=False) as archive:
                for path, name in candidates:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(path, flags)
                    with os.fdopen(descriptor, "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        total += info.st_size
                        if total > MAX_CHECKPOINT_SOURCE_BYTES:
                            raise ValueError("archive exceeds source byte limit")
                        digest = hashlib.sha256()
                        with tempfile.SpooledTemporaryFile(
                            max_size=1024 * 1024,
                            mode="w+b",
                            dir="/tmp",
                        ) as stable:
                            copied = 0
                            for chunk in iter(
                                lambda: source.read(COPY_CHUNK_BYTES), b""
                            ):
                                stable.write(chunk)
                                digest.update(chunk)
                                copied += len(chunk)
                            if copied != info.st_size:
                                raise ValueError("archive source changed while reading")
                            stable.seek(0)
                            member = tarfile.TarInfo(name)
                            member.size = copied
                            member.mtime = int(info.st_mtime)
                            member.mode = stat.S_IMODE(info.st_mode)
                            archive.addfile(member, stable)
                        entries.append((name, copied, digest.hexdigest()))
                manifest = json.dumps(
                    {
                        "version": 1,
                        "root": root.relative_to(WORKSPACE).as_posix(),
                        "file_count": len(entries),
                        "total_bytes": total,
                        "files": [
                            {"path": name, "size_bytes": size, "sha256": digest}
                            for name, size, digest in entries
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                manifest_info = tarfile.TarInfo("manifest.json")
                manifest_info.size = len(manifest)
                manifest_info.mtime = 0
                manifest_info.mode = 0o644
                archive.addfile(manifest_info, io.BytesIO(manifest))
            size = buffer.tell()
            if size > MAX_CHECKPOINT_BYTES:
                raise ValueError("compressed archive exceeds size limit")
            buffer.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: buffer.read(COPY_CHUNK_BYTES), b""):
                digest.update(chunk)
            buffer.seek(0)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-SHA256", digest.hexdigest())
            self.send_header("X-File-Count", str(len(entries)))
            self.send_header("X-Source-Bytes", str(total))
            self.end_headers()
            shutil.copyfileobj(buffer, self.wfile, COPY_CHUNK_BYTES)

    @staticmethod
    def _safe_member_path(name: str) -> Path:
        relative = PurePosixPath(name)
        if (
            not name
            or not relative.parts
            or relative == PurePosixPath(".")
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise ValueError("checkpoint contains an unsafe path")
        if relative.parts[0] == ".sandbox":
            raise ValueError("checkpoint contains a reserved path")
        return Path(*relative.parts)

    @staticmethod
    def _swap_in_restored_tree(staging: Path, retired: Path) -> None:
        """Swap the staged snapshot in, keeping the old tree until it works.

        The old data is *moved* aside rather than deleted: staging lives on the
        same PVC, so the renames below can still fail halfway (ENOSPC, EIO), and
        purge-then-move loses everything that had not been moved yet. Renaming
        first means every failure is recoverable — the old tree goes back and
        the caller sees an error instead of a half-empty workspace.
        """
        installed: list[str] = []
        # The log must be older than the first rename: any moment in the following two rounds may be SIGKILL
        # (Control Plane deletes Pod using gracePeriodSeconds: 0), and SIGKILL cannot reach the following
        # except. The log was the only clue that was still alive in that situation, see
        # recover_interrupted_restore()。
        _write_restore_journal("retiring", staging, retired)
        try:
            for child in list(WORKSPACE.iterdir()):
                if child.name == ".sandbox":
                    continue
                os.replace(child, retired / child.name)
            _write_restore_journal("installing", staging, retired)
            for child in list(staging.iterdir()):
                os.replace(child, WORKSPACE / child.name)
                installed.append(child.name)
        except BaseException:
            # Undo in reverse order: pull the partially installed snapshot back
            # out first, otherwise a name present in both trees would block the
            # old entry from returning.
            for name in installed:
                try:
                    os.replace(WORKSPACE / name, staging / name)
                except OSError:
                    pass
            # After pulling it back, the shape on the disk changed back to "just the old tree removed". The phase changes accordingly
            # Go back: In case the next round also hangs (or the process happens to be killed here), press the self-heal button
            # Retiring is the right ending - leaving installing will let it pour staging in
            # The workspace of the old entry has been put back, and the file with the same name has been overwritten by the new one.
            _write_restore_journal("retiring", staging, retired)
            for child in list(retired.iterdir()):
                os.replace(child, WORKSPACE / child.name)
            _clear_restore_journal()
            raise
        _clear_restore_journal()

    def restore_checkpoint(self, archive_file) -> dict[str, int]:
        """Validate and stage a complete snapshot before replacing user data.

        archive_file is a seek(0)ed binary file object (spooled on disk, see
        handle_restore) - not bytes: Full load will pull the RSS to the same size as the archive,
        The 128Mi container cannot accommodate the archive with the 256MB limit."""
        files = 0
        total = 0
        with tarfile.open(fileobj=archive_file, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_CHECKPOINT_ENTRIES:
                raise ValueError("checkpoint contains too many entries")
            validated: list[tuple[tarfile.TarInfo, Path]] = []
            seen: set[Path] = set()
            for member in members:
                relative = self._safe_member_path(member.name)
                if relative in seen:
                    raise ValueError("checkpoint contains duplicate paths")
                seen.add(relative)
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError("checkpoint contains unsupported links or devices")
                if member.isfile():
                    total += int(member.size)
                    files += 1
                    if total > MAX_CHECKPOINT_SOURCE_BYTES:
                        raise ValueError("checkpoint expands beyond size limit")
                elif not member.isdir():
                    raise ValueError("checkpoint contains an unsupported entry")
                validated.append((member, relative))

            metadata = WORKSPACE / ".sandbox"
            metadata.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="restore-", dir=metadata))
            retired = Path(tempfile.mkdtemp(prefix="old-", dir=metadata))
            swapped = False
            try:
                for member, relative in validated:
                    target = staging / relative
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("checkpoint file content is missing")
                    # Chunked copy: a member can be hundreds of MB, read() will read it in whole
                    # memory, and this container only has 512Mi.
                    atomic_copy(target, source)

                self._swap_in_restored_tree(staging, retired)
                swapped = True
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                if swapped:
                    shutil.rmtree(retired, ignore_errors=True)
                elif not any(retired.iterdir()):
                    # Rollback successful = old data has been returned to the workspace, and empty directories can be deleted.
                    # It is also deliberately left on when rollback fails: this is the only remaining copy of the user data.
                    retired.rmdir()
        return {"files": files, "bytes": total}


class _OperationCapture(ApiHandler):
    """Run the HTTP handler's workspace operations without an HTTP socket.

    Runtime MCP uses this adapter so the HTTP compatibility API and MCP tools
    share one implementation of path confinement, traversal budgets, atomic
    writes, and response contracts.  Keeping those rules in one place is more
    important than keeping the handler methods cosmetically transport-free.
    """

    def __init__(self) -> None:
        # BaseHTTPRequestHandler.__init__ immediately starts reading a socket.
        self.result: dict | None = None

    def send_json(self, status: int, payload: object) -> None:
        if status != HTTPStatus.OK:
            raise RuntimeError(f"workspace operation returned HTTP {status}")
        if not isinstance(payload, dict):
            raise RuntimeError("workspace operation returned a non-object payload")
        self.result = payload


def execute_file_operation(operation: str, arguments: dict) -> dict:
    """Execute one Runtime MCP file tool using the canonical workspace rules."""

    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    handler = _OperationCapture()
    path = arguments.get("path", "")
    if operation == "file_read":
        handler.handle_read(
            path,
            int(arguments.get("offset", 1) or 1),
            int(arguments.get("limit", 0) or 0),
        )
    elif operation == "file_write":
        handler.handle_write(arguments)
    elif operation == "file_edit":
        handler.handle_edit(arguments)
    elif operation == "file_glob":
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be non-empty")
        handler.handle_glob(
            path,
            pattern,
            int(arguments.get("limit", 0) or 0),
        )
    elif operation == "file_grep":
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str):
            raise ValueError("pattern must be a string")
        handler.handle_grep(
            path,
            pattern,
            arguments.get("glob", "") or "",
            arguments.get("mode", "files_with_matches") or "files_with_matches",
            bool(arguments.get("case_insensitive", False)),
            int(arguments.get("context", 0) or 0),
            bool(arguments.get("regex", False)),
            int(arguments.get("limit", 0) or 0),
        )
    else:
        raise ValueError(f"unknown file operation: {operation}")
    if handler.result is None:
        raise RuntimeError("workspace operation produced no result")
    record_activity()
    return handler.result


if __name__ == "__main__":
    # When the log pipe is full, it is better to lose the log than freeze the thread (see safe_stdout module and 2026-08-17
    # fault): Once the consumer side is shut down, the print() of any thread will be permanently blocked - in this process
    # Each response requires a line of logs, so all HTTP threads are frozen together with /healthz
    # On print, the probe times out and restarts, and restarting cannot repair the suspended consumer.
    # Only installed at the service process entrance: single test and gc_workspaces.py directly use native stdout.
    # safe_stdout ships inside the image as sandbox_platform/safe_stdout.py (see file-service/Dockerfile COPY).
    # So the build context of file-service is the repository root instead of file-service/.
    from sandbox_platform import safe_stdout

    safe_stdout.install()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    recovery = recover_interrupted_restore()
    if recovery is not None:
        print(json.dumps({"event": "restore_recovered", **recovery}, sort_keys=True), flush=True)
    record_activity()
    fingerprint = hashlib.sha256(CAPABILITY_KEY.encode("utf-8")).hexdigest()[:8]
    print(
        f"file service listening on {HOST}:{PORT}, "
        f"workspace={WORKSPACE_ID}, token fingerprint={fingerprint}",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), ApiHandler).serve_forever()
