"""Session-scoped client for Runtime MCP and explicit object transfers.

There is deliberately no local filesystem or subprocess fallback. If Control Plane or
Runtime is unavailable, tools return an explicit error to the Agent.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import hashlib
import json
import os
import queue
import re
import shlex
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sandbox_platform import __version__
from sandbox_platform.control_plane_transport import ControlPlaneError, ControlPlaneTransport

PROTOCOL_VERSION = "2026-07-28"
# Upper bound for settling interrupted SSE reader threads. After shutdown,
# readline should return immediately; five seconds is headroom under load.
_READER_JOIN_SECONDS = 5.0
#The number of reader threads that are still blocked after interruption. Zero cost accounting: If this number is not zero, it means there are threads
#It's leaked along with its socket and can't be seen anywhere else.
_SSE_READER_LEAKS = 0
_SESSION_KEY = contextvars.ContextVar(
    "sandbox_session_key",
    default=None,
)
#: The pseudonymous subject every request from this scope acts for.
#:
#: A context variable rather than an attribute on the manager, because the
#: manager is a process-wide singleton and this is per end user. Held on the
#: singleton, one request handler binding a subject would change the identity of
#: every request in flight in every other thread - and a request sent under the
#: wrong subject is indistinguishable from one sent under the right one, in the
#: response, in the logs, and in where the object ended up.
#:
#: Deliberately not a parameter on each call either: a header threaded through
#: N call sites is N places to forget it, and forgetting is silent - the request
#: simply arrives as though the client acted for nobody, which is exactly what a
#: client that legitimately acts for nobody looks like.
_ACTING_SUBJECT = contextvars.ContextVar(
    "sandbox_acting_subject",
    default=None,
)
#: The shape the platform accepts, asserted here so a derivation mistake is
#: reported where the derivation is, not as a 400 on some later call.
_ACTING_SUBJECT_SHAPE = re.compile(r"^[0-9a-f]{32}$")
#Whether the current scope has a bound Runtime. False when the child agent borrows the parent workspace.
#
#AI-LOCK: The default True is intentional - libraries/direct calls without ambient binding are handled as "own",
#consistent with an embedding host falling back to owns=True when it cannot resolve the ambient session key.
#Changing it to the default False will prevent the caller using sandbox_client directly from releasing its own runtime.
_OWNS_RUNTIME = contextvars.ContextVar(
    "sandbox_owns_runtime",
    default=True,
)


@dataclasses.dataclass
class Lease:
    session_id: str
    workspace_id: str | None = None
    workspace_owner: str | None = None
    workspace_token: str | None = None
    workspace_token_expires_at: float = 0.0
    workspace_checked_at: float = 0.0
    workspace_created: bool | None = None
    sandbox_id: str | None = None
    sandbox_token: str | None = None
    sandbox_token_expires_at: float = 0.0
    sandbox_checked_at: float = 0.0
    #The template actually used by the current runtime. Once the runtime is built, the image is fixed: change the template
    #It must be released first and then created, so remember here that it is used to block "thinking the switch is successful".
    sandbox_template: str | None = None


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """Result of a non-interactive command executed in a Runtime."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False


@contextlib.contextmanager
def session_context(session_key: str, *, owns: bool = True):
    """Bind the sandbox session; ``owns`` declares whether this scope owns the runtime.

    Borrow the child agent of the parent workspace and pass owns=False - it can read and write /workspace and
    Run the command, but cannot release the Runtime, because the parent or sibling may still be used.

    This binds **which workspace** the work goes in, not **whose** it is. An
    object is filed under a subject as well, and that comes from
    ``acting_subject_context``. The two are deliberately separate - a
    management-plane caller legitimately binds only this one - so this sentence
    is here for the reader who arrived at this function and has no reason to
    know the other exists."""
    token = _SESSION_KEY.set(session_key)
    owns_token = _OWNS_RUNTIME.set(owns)
    try:
        yield
    finally:
        _SESSION_KEY.reset(token)
        _OWNS_RUNTIME.reset(owns_token)


@contextlib.contextmanager
def acting_subject_context(subject: str):
    """Bind the pseudonymous subject this scope acts for.

    ``subject`` is 32 lowercase hexadecimal characters, **already derived** by
    whoever holds the salt. This client does not derive it and must not: hashing
    a pseudonym a second time yields another perfectly well-formed pseudonym, so
    the person the caller named and the person the platform records become two
    different people while both sides answer 2xx and nothing reports it.

    Separate from ``session_context`` on purpose. A session is which workspace
    the work goes in; a subject is who it belongs to. They are usually bound
    together, but the platform partitions objects by the subject alone, and
    conflating them would make one of the two impossible to set on its own.
    """
    if not isinstance(subject, str) or not _ACTING_SUBJECT_SHAPE.fullmatch(subject):
        # Refused here rather than passed on. Every wrong derivation produces a
        # plausible-looking value: truncating the hex string instead of the
        # digest gives 16 characters, and uppercase hex is the same bytes. Both
        # are visible in this check and in nothing else the caller runs.
        raise ValueError(
            "acting subject must be 32 lowercase hexadecimal characters "
            "(the first 16 bytes of the digest, rendered as hex); "
            f"got {subject!r}"
        )
    token = _ACTING_SUBJECT.set(subject)
    try:
        yield
    finally:
        _ACTING_SUBJECT.reset(token)


def current_acting_subject() -> str | None:
    """The subject this scope acts for, or None when it acts for nobody."""
    return _ACTING_SUBJECT.get()


def owns_current_runtime() -> bool:
    """Whether the current scope has the right to release the bound runtime."""
    return _OWNS_RUNTIME.get()


def current_session_key() -> str:
    session_key = _SESSION_KEY.get()
    if session_key:
        return session_key
    configured = os.getenv("SANDBOX_SESSION_ID")
    if configured:
        return configured
    raise RuntimeError(
        "sandbox session context is not bound; wrap the call in "
        "session_context(...) or set SANDBOX_SESSION_ID (any stable string that "
        "identifies this agent session; it selects the Workspace)"
    )


def is_configured() -> bool:
    """Return whether this process has credentials for the optional Control Plane."""
    return bool(
        os.getenv("SANDBOX_CONTROL_PLANE_URL", "").strip()
        and os.getenv("SANDBOX_TOKEN", "").strip()
    )

def normalize_workspace_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if path == "/workspace":
        return ""
    if path.startswith("/workspace/"):
        return path[len("/workspace/"):]
    if path.startswith("/"):
        raise ValueError("only /workspace paths or relative paths are allowed")
    if path.startswith("~/") or path == "~":
        raise ValueError("home paths are not available; use /workspace")
    return path


class SandboxManager:
    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()
        self._request_id = 0

    @property
    def control_plane_url(self) -> str:
        return os.getenv(
            "SANDBOX_CONTROL_PLANE_URL", "http://127.0.0.1:18080"
        ).rstrip("/")

    @property
    def control_plane_token(self) -> str:
        token = os.getenv("SANDBOX_TOKEN")
        if not token:
            raise RuntimeError(
                "SANDBOX_TOKEN is required; local tool fallback is disabled"
            )
        return token

    @property
    def lifecycle_refresh_seconds(self) -> int:
        raw = os.getenv("SANDBOX_LIFECYCLE_REFRESH_SECONDS", "60")
        try:
            return max(5, int(raw))
        except ValueError as exc:
            raise RuntimeError(
                "SANDBOX_LIFECYCLE_REFRESH_SECONDS must be an integer"
            ) from exc

    def _session_id(self, session_key: str) -> str:
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]

    def _lease(self, session_key: str | None = None) -> Lease:
        key = session_key or current_session_key()
        lease = self._leases.get(key)
        if lease is None:
            lease = Lease(session_id=self._session_id(key))
            self._leases[key] = lease
        return lease

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        token: str | None = None,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 100.0,
    ) -> tuple[dict, str]:
        self._require_object_identity(path, payload, query)
        subject = current_acting_subject()
        return ControlPlaneTransport(
            self.control_plane_url,
            token or self.control_plane_token,
            default_headers=(
                {} if subject is None else {"X-Acting-Subject": subject}
            ),
        ).request(
            method,
            path,
            payload=payload,
            query=query,
            headers=headers,
            timeout=timeout,
        )

    @staticmethod
    def _require_object_identity(
        path: str, payload: dict | None, query: dict[str, str] | None
    ) -> None:
        """Refuse an object call that names nobody, before it is sent.

        An object is filed under ``<tenant>/<subject>``: the platform takes the
        tenant from the credential and the subject from ``X-Acting-Subject``,
        and refuses a request that supplies either. So a call with no bound
        subject has no partition to go in, and the platform answers 400.

        That 400 is correct and useless. By the time it arrives every piece of
        the causal chain is gone - which call site, for which user, and why
        nothing was bound. The same fault stated here is "this code path did not
        bind a subject", and it names the operation. An error belongs in the
        layer that can still say why it happened.

        A management-plane credential is the exception rather than a hole in
        this: it has no tenant of its own, so naming an owner outright is the
        only way it can act for one, and a call that does name one needs no
        subject.
        """
        if not (
            path.startswith("/v1/storage/")
            or path.endswith(("/objects/import", "/objects/export"))
        ):
            return
        if (payload or {}).get("owner") is not None:
            return
        if (query or {}).get("owner") is not None:
            return
        if current_acting_subject() is not None:
            return
        raise RuntimeError(
            f"{path} files an object under <tenant>/<subject> and this scope "
            "binds no subject; wrap the call in acting_subject_context(...) "
            "with the pseudonym derived for this end user, or pass an explicit "
            "owner if this client holds a management-plane credential"
        )

    def ping(self) -> None:
        result, _ = self._request("GET", "/healthz", timeout=5)
        if result.get("status") != "ok":
            raise ControlPlaneError(503, "Sandbox Control Plane is not ready")

    @staticmethod
    def _object_id(value: str) -> str:
        normalized = "".join(
            char if char.isascii() and char.isalnum() else "-"
            for char in value.lower()
        ).strip("-")
        normalized = "-".join(part for part in normalized.split("-") if part)
        if not normalized or len(normalized) > 63:
            normalized = f"id-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"
        return normalized

    def put_agent_blob(
        self,
        agent_id: str,
        run_id: str,
        path: str,
        content: str | bytes,
        *,
        content_type: str = "application/octet-stream",
        owner: str | None = None,
    ) -> dict:
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(data).hexdigest()
        # No owner unless the caller names one. The partition is
        # <tenant>/<subject>, both segments derived by the platform from this
        # client's credential and its X-Acting-Subject header, and a
        # tenant-bound credential that sends one is refused rather than
        # corrected. Only the management plane, which has no tenant of its own,
        # has an owner to name.
        location = {
            "scope": "agent",
            "agent_id": self._object_id(agent_id),
            "run_id": self._object_id(run_id),
            "path": path,
        }
        if owner is not None:
            location["owner"] = owner
        ticket, _ = self._request(
            "POST",
            "/v1/storage/tickets",
            payload={
                **location,
                "operation": "upload",
                "max_bytes": max(1, len(data)),
                "content_type": content_type,
                "sha256": digest,
            },
        )
        request = urllib.request.Request(
            f"{self.control_plane_url}{ticket['url']}",
            data=data,
            headers={
                "Authorization": f"Bearer {ticket['access_token']}",
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
            },
            method=ticket["method"],
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ControlPlaneError(exc.code, "MinIO blob upload failed") from exc
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ControlPlaneError(502, "MinIO blob upload failed") from exc
        return {**location, **result}

    @staticmethod
    def _object_query(locator: dict) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in locator.items()
            if value not in (None, "")
        }

    def issue_object_ticket(
        self,
        locator: dict,
        *,
        operation: str,
        max_bytes: int,
        content_type: str,
        sha256: str = "",
        expires_in: int = 120,
    ) -> dict:
        payload = {
            **locator,
            "operation": operation,
            "max_bytes": max_bytes,
            "content_type": content_type,
            "expires_in": expires_in,
        }
        if sha256:
            payload["sha256"] = sha256
        result, _ = self._request(
            "POST",
            "/v1/storage/tickets",
            payload=payload,
        )
        return result

    def open_object(
        self,
        locator: dict,
        *,
        max_bytes: int,
        content_type: str,
        expires_in: int = 120,
    ) -> object:
        """Sign a download ticket and open the object byte stream, returning the unconsumed response object.

        Deliberately not built on ``_request``: it will take the entire body ``.read()`` and press
        UTF-8 decode, which corrupts a binary payload outright. Here the original response of ``urlopen`` is given to
        The caller, which is streamed; error semantics are aligned with ``_request`` (HTTPError with upstream
        Status changes to ControlPlaneError, failure to connect changes to 502).

        The ticket is a single consumption: any retry after failure must re-adjust this method and re-sign the ticket.
        Instead of replaying the old access_token."""
        ticket = self.issue_object_ticket(
            locator,
            operation="download",
            max_bytes=max_bytes,
            content_type=content_type,
            expires_in=expires_in,
        )
        request = urllib.request.Request(
            f"{self.control_plane_url}/v1/storage/content",
            headers={"Authorization": f"Bearer {ticket['access_token']}"},
            method="GET",
        )
        try:
            return urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("error")
            except (json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise ControlPlaneError(
                exc.code,
                detail or f"Control Plane returned HTTP {exc.code}",
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise ControlPlaneError(
                502, f"Sandbox Control Plane unavailable at {self.control_plane_url}: {exc}"
            ) from exc

    def stat_object(self, locator: dict) -> dict:
        result, _ = self._request(
            "GET",
            "/v1/storage/objects/stat",
            query=self._object_query(locator),
        )
        return result

    def list_objects(self, locator: dict) -> dict:
        result, _ = self._request(
            "GET",
            "/v1/storage/objects/list",
            query=self._object_query(locator),
        )
        return result

    def delete_object(
        self,
        locator: dict,
        *,
        purge_versions: bool = False,
    ) -> dict:
        query = self._object_query(locator)
        if purge_versions:
            query["purge_versions"] = "true"
        result, _ = self._request(
            "DELETE",
            "/v1/storage/objects",
            query=query,
        )
        return result

    def import_object_to_workspace(
        self,
        locator: dict,
        destination: str,
        *,
        session_key: str | None = None,
    ) -> dict:
        with self._lock:
            #The owner in the locator comes from the authentication identity, bring it into the workspace token:
            #After this token, only the object prefix of this owner can be read and written.
            lease = self.ensure_workspace(
                session_key,
                owner=locator.get("owner"),
            )
            #Control Plane's import requires Runtime MCP to write to the real Workspace. Upload occurs in
            #Before the first message of the session - the Runtime has not started yet naturally at that time. If it is not ensured here,
            #The commit will transparently transmit the workspace_error as it is, and the front-end attachment chip will directly enter the failure state.
            #(Actual test: "object import requires a running Runtime for ws-…").
            #The lease is cached by session_key, and the first tool of the Agent reuses the same lease, so there will be no more Pods;
            #Above, we first signed the workspace token by owner, here owner=None cache check
            #In constant hit, the owner binding will not be washed away.
            self.ensure_runtime(session_key)
            assert lease.workspace_id and lease.workspace_token
            result, _ = self._request(
                "POST",
                f"/v1/workspaces/{lease.workspace_id}/objects/import",
                payload={**locator, "destination": destination},
                token=lease.workspace_token,
            )
            return result

    def export_workspace_object(
        self,
        workspace_path: str,
        locator: dict,
        *,
        session_key: str | None = None,
    ) -> dict:
        with self._lock:
            lease = self.ensure_workspace(
                session_key,
                owner=locator.get("owner"),
            )
            assert lease.workspace_id and lease.workspace_token
            result, _ = self._request(
                "POST",
                f"/v1/workspaces/{lease.workspace_id}/objects/export",
                payload={**locator, "workspace_path": workspace_path},
                token=lease.workspace_token,
            )
            return result

    def export_workspace_collection(
        self,
        workspace_path: str,
        locator: dict,
        *,
        session_key: str | None = None,
    ) -> dict:
        """Archive one directory and export it as a single durable object."""
        with self._lock:
            lease = self.ensure_workspace(
                session_key,
                owner=locator.get("owner"),
            )
            assert lease.workspace_id and lease.workspace_token
            result, _ = self._request(
                "POST",
                f"/v1/workspaces/{lease.workspace_id}/objects/export",
                payload={
                    **locator,
                    "workspace_path": workspace_path,
                    "archive": True,
                },
                token=lease.workspace_token,
                timeout=150,
            )
            return result

    def checkpoint_workspace(self, session_key: str | None = None) -> dict:
        with self._lock:
            lease = self.ensure_workspace(session_key)
            assert lease.workspace_id
            result, _ = self._request(
                "POST",
                f"/v1/workspaces/{lease.workspace_id}/checkpoints",
                payload={},
                timeout=150,
            )
            return result

    def list_workspace_checkpoints(
        self,
        session_key: str | None = None,
    ) -> dict:
        with self._lock:
            lease = self.ensure_workspace(session_key)
            assert lease.workspace_id
            result, _ = self._request(
                "GET",
                f"/v1/workspaces/{lease.workspace_id}/checkpoints",
            )
            return result

    def delete_workspace_checkpoint(
        self,
        checkpoint_id: str,
        *,
        session_key: str | None = None,
    ) -> dict:
        with self._lock:
            lease = self.ensure_workspace(session_key)
            assert lease.workspace_id
            result, _ = self._request(
                "DELETE",
                (
                    f"/v1/workspaces/{lease.workspace_id}/checkpoints/"
                    f"{checkpoint_id}"
                ),
            )
            return result

    def restore_workspace(
        self,
        checkpoint_id: str,
        *,
        sha256: str | None = None,
        session_key: str | None = None,
    ) -> dict:
        with self._lock:
            lease = self.ensure_workspace(session_key)
            assert lease.workspace_id
            result, _ = self._request(
                "POST",
                (
                    f"/v1/workspaces/{lease.workspace_id}/checkpoints/"
                    f"{checkpoint_id}/restore"
                ),
                payload={"sha256": sha256} if sha256 else {},
                timeout=150,
            )
            return result

    def ensure_workspace(
        self,
        session_key: str | None = None,
        *,
        owner: str | None = None,
        refresh: bool = False,
    ) -> Lease:
        """Make sure the session has a workspace and scoped token available.

        ``owner`` is the object prefix this token is allowed to touch
        (``<tenant>/<subject>``), and only a management-plane credential may
        name one - for everybody else the platform derives it from the
        credential and ``X-Acting-Subject``, reports it back as ``owner``, and
        refuses a request that names one. A cached token is re-signed when a
        named owner differs from the one it was issued for, because the
        platform checks an object operation against the token's owner."""
        with self._lock:
            lease = self._lease(session_key)
            now = time.time()
            if (
                not refresh
                and lease.workspace_id
                and lease.workspace_token
                and lease.workspace_token_expires_at > now + 60
                and lease.workspace_checked_at
                > now - self.lifecycle_refresh_seconds
                and (owner is None or lease.workspace_owner == owner)
            ):
                return lease
            payload = {"session_id": lease.session_id}
            if owner is not None:
                payload["owner"] = owner
            result, _ = self._request(
                "POST",
                "/v1/workspaces",
                payload=payload,
            )
            lease.workspace_id = result["workspace_id"]
            # An older Control Plane does not return this field. Preserve its
            # historical restore behavior during a rolling upgrade; the new
            # server always sends an explicit boolean.
            lease.workspace_created = result.get("created") is not False
            lease.workspace_owner = result.get("owner")
            lease.workspace_token = result["access_token"]
            lease.workspace_token_expires_at = (
                time.time() + int(result.get("access_token_expires_in", 600))
            )
            lease.workspace_checked_at = time.time()
            return lease

    def ensure_runtime(
        self,
        session_key: str | None = None,
        *,
        template: str | None = None,
    ) -> Lease:
        """Make sure the session has a runtime available.

        ``template`` selects the image template in the Control Plane registry. None means using the platform default.
        After the runtime is built, the image is immutable, so reusing a runtime that does not conform to the template will only report an error:
        Silent reuse will make the caller think that he has changed the environment, but what he gets is the image of the previous template——
        This kind of failure will not be exposed until the command is actually run, which is much harder to detect than a direct refusal here.
        The normal path to change the template is to create again after release."""
        with self._lock:
            lease = self.ensure_workspace(session_key)
            if (
                lease.sandbox_id
                and template is not None
                and lease.sandbox_template != template
            ):
                raise ControlPlaneError(
                    409,
                    f"sandbox already running with template "
                    f"{lease.sandbox_template!r}; release it before "
                    f"switching to {template!r}",
                )
            if lease.sandbox_id:
                now = time.time()
                if (
                    lease.sandbox_token_expires_at <= now + 60
                    or lease.sandbox_checked_at
                    <= now - self.lifecycle_refresh_seconds
                ):
                    try:
                        result, _ = self._request(
                            "POST",
                            f"/v1/sandboxes/{lease.sandbox_id}/token",
                            payload={},
                        )
                    except ControlPlaneError as exc:
                        if exc.status != 404:
                            raise
                        lease.sandbox_id = None
                        lease.sandbox_token = None
                        lease.sandbox_token_expires_at = 0
                        lease.sandbox_checked_at = 0
                        #The runtime has been recycled by the control_plane, and the template records must be cleared accordingly.
                        #Otherwise, when rebuilding below, the old template will be compared to a non-existent sandbox.
                        lease.sandbox_template = None
                    else:
                        lease.sandbox_token = result["access_token"]
                        lease.sandbox_token_expires_at = (
                            time.time()
                            + int(result.get("access_token_expires_in", 600))
                        )
                        lease.sandbox_checked_at = time.time()
                if lease.sandbox_id:
                    return lease
            payload = {"workspace_id": lease.workspace_id}
            if template is not None:
                payload["template_id"] = template
            result, _ = self._request("POST", "/v1/sandboxes", payload=payload)
            lease.sandbox_id = result["id"]
            lease.sandbox_token = result["access_token"]
            lease.sandbox_token_expires_at = (
                time.time() + int(result.get("access_token_expires_in", 600))
            )
            lease.sandbox_checked_at = time.time()
            #The template returned by the Control Plane shall prevail rather than the one in the request: the request can be made without template
            #Using the platform default, only the response knows which one will take effect in the end.
            lease.sandbox_template = result.get("template")
            return lease

    def list_runtimes(self) -> list[dict]:
        """Return active Runtimes visible to the configured Control Plane credential."""
        result, _ = self._request("GET", "/v1/sandboxes")
        runtimes = result.get("sandboxes")
        if not isinstance(runtimes, list):
            raise ControlPlaneError(502, "Control Plane response has no sandboxes list")
        return [runtime for runtime in runtimes if isinstance(runtime, dict)]

    def list_workspaces(self) -> list[dict]:
        """Return persistent Workspaces visible to the Control Plane credential."""
        result, _ = self._request("GET", "/v1/workspaces")
        workspaces = result.get("workspaces")
        if not isinstance(workspaces, list):
            raise ControlPlaneError(502, "Control Plane response has no workspaces list")
        return [workspace for workspace in workspaces if isinstance(workspace, dict)]

    def resolve_workspace(self, session_key: str) -> tuple[Lease, dict]:
        """Resolve a name without creating or enumerating Workspaces."""
        lease = self._lease(session_key)
        result, _ = self._request(
            "POST",
            "/v1/workspaces/resolve",
            payload={"session_id": lease.session_id},
        )
        workspace_id = result.get("workspace_id")
        if not isinstance(workspace_id, str):
            raise ControlPlaneError(502, "Control Plane returned a workspace without an id")
        lease.workspace_id = workspace_id
        lease.workspace_checked_at = time.time()
        return lease, result

    def lookup_runtime(self, session_key: str) -> Lease:
        """Find an active Runtime for a named persistent Workspace.

        The resolver is read-only. This lets a fresh CLI process rediscover a
        Runtime without creating an empty Workspace on a miss, enumerating
        session keys, or persisting scoped tokens locally.
        """
        with self._lock:
            lease, resolved = self.resolve_workspace(session_key)
            sandbox_id = resolved.get("sandbox_id")
            if not isinstance(sandbox_id, str):
                raise ControlPlaneError(404, f"sandbox {session_key!r} is not running")
            token, _ = self._request(
                "POST",
                f"/v1/sandboxes/{sandbox_id}/token",
                payload={},
            )
            lease.sandbox_id = sandbox_id
            lease.sandbox_token = token["access_token"]
            lease.sandbox_token_expires_at = time.time() + int(
                token.get("access_token_expires_in", 600)
            )
            lease.sandbox_checked_at = time.time()
            lease.sandbox_template = resolved.get("template")
            return lease

    def read_file(self, path: str, offset: int = 1, limit: int = 0) -> dict:
        arguments = {"path": normalize_workspace_path(path)}
        if offset and offset > 1:
            arguments["offset"] = offset
        if limit and limit > 0:
            arguments["limit"] = limit
        return self._runtime_tool_call("file_read", arguments)

    def glob_files(self, pattern: str, path: str = "", limit: int = 0) -> dict:
        arguments = {
            "path": normalize_workspace_path(path) if path else "",
            "pattern": pattern,
        }
        if limit and limit > 0:
            arguments["limit"] = limit
        return self._runtime_tool_call("file_glob", arguments)

    def grep_files(
        self,
        pattern: str,
        path: str = "",
        file_glob: str = "",
        mode: str = "files_with_matches",
        case_insensitive: bool = False,
        context: int = 0,
        use_regex: bool = False,
        limit: int = 0,
    ) -> dict:
        arguments = {
            "path": normalize_workspace_path(path) if path else "",
            "pattern": pattern,
            "mode": mode,
        }
        if file_glob:
            arguments["glob"] = file_glob
        if case_insensitive:
            arguments["case_insensitive"] = True
        if context:
            arguments["context"] = context
        if use_regex:
            arguments["regex"] = True
        if limit and limit > 0:
            arguments["limit"] = limit
        return self._runtime_tool_call("file_grep", arguments)

    def write_file(self, path: str, content: str) -> dict:
        return self._runtime_tool_call(
            "file_write",
            {
                "path": normalize_workspace_path(path),
                "content": content,
            },
        )

    def edit_file(self, path: str, old: str, new: str) -> dict:
        return self._runtime_tool_call(
            "file_edit",
            {
                "path": normalize_workspace_path(path),
                "old": old,
                "new": new,
            },
        )

    def _runtime_tool_call(
        self,
        name: str,
        arguments: dict,
        *,
        timeout_seconds: int = 30,
    ) -> dict:
        lease = self.ensure_runtime()
        assert lease.sandbox_id and lease.sandbox_token
        with self._lock:
            self._request_id += 1
            request_id = f"sandbox-client-{self._request_id}-{uuid.uuid4().hex[:6]}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/client": {
                    "name": "sandbox-client",
                    "version": __version__,
                },
            },
        }
        result, content_type = self._request(
            "POST",
            f"/v1/sandboxes/{lease.sandbox_id}/mcp",
            payload=payload,
            token=lease.sandbox_token,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": name,
            },
            timeout=timeout_seconds + 15,
        )
        if "text/event-stream" in content_type:
            result = self._parse_sse_result(result.get("raw", ""), request_id)
        if "error" in result:
            error = result["error"]
            raise ControlPlaneError(
                502,
                f"MCP {error.get('code')}: {error.get('message')}",
            )
        tool_result = result.get("result") or {}
        structured = tool_result.get("structuredContent")
        if not isinstance(structured, dict):
            raise ControlPlaneError(502, "MCP response has no structuredContent")
        return structured

    def shell(self, command: str, timeout_seconds: int = 30) -> dict:
        return self._runtime_tool_call(
            "shell",
            {
                "action": "exec",
                "command": command,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )

    def shell_stream(
        self,
        command: str,
        timeout_seconds: int = 30,
        on_chunk=None,
        is_cancelled=None,
    ) -> dict:
        """Execute Shell MCP and deliver stdout/stderr progress incrementally.

        Runtime and Control Plane already preserve MCP ``notifications/progress`` as
        flushed SSE frames.  Reading the response line-by-line here is the
        critical difference from :meth:`shell`, whose generic request helper
        buffers the whole response before parsing it.
        """
        return self._shell_stream_request(
            {
                "action": "exec_stream",
                "command": command,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
            on_chunk=on_chunk,
            is_cancelled=is_cancelled,
        )

    def shell_session(
        self,
        action: str,
        session_id: str,
        *,
        command: str = "",
        timeout_seconds: int = 30,
        input_text: str = "",
        append_newline: bool = True,
        async_mode: bool = False,
        on_chunk=None,
        is_cancelled=None,
    ) -> dict:
        """Operate one Runtime-local persistent PTY session over MCP SSE."""
        if action not in {"exec", "wait", "input", "kill"}:
            raise ValueError("action must be exec, wait, input, or kill")
        arguments = {
            "action": f"session_{action}",
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
        }
        if action == "exec":
            arguments.update({"command": command, "async": async_mode})
        elif action == "input":
            arguments.update({
                "input": input_text,
                "append_newline": append_newline,
            })
        return self._shell_stream_request(
            arguments,
            timeout_seconds=timeout_seconds,
            on_chunk=on_chunk,
            is_cancelled=is_cancelled,
        )

    def _shell_stream_request(
        self,
        arguments: dict,
        *,
        timeout_seconds: int,
        on_chunk=None,
        is_cancelled=None,
    ) -> dict:
        lease = self.ensure_runtime()
        assert lease.sandbox_id and lease.sandbox_token
        with self._lock:
            self._request_id += 1
            request_id = f"sandbox-client-{self._request_id}-{uuid.uuid4().hex[:6]}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "shell",
                "arguments": arguments,
            },
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/client": {
                    "name": "sandbox-client",
                    "version": __version__,
                },
            },
        }
        request = urllib.request.Request(
            f"{self.control_plane_url}/v1/sandboxes/{lease.sandbox_id}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "text/event-stream, application/json",
                "Authorization": f"Bearer {lease.sandbox_token}",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": "shell",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds + 15,
            ) as response:
                content_type = response.headers.get(
                    "Content-Type", "application/json"
                )
                if "text/event-stream" in content_type:
                    result = self._read_sse_stream(
                        response,
                        request_id,
                        on_chunk,
                        is_cancelled,
                    )
                else:
                    raw = response.read()
                    try:
                        result = json.loads(raw) if raw else {}
                    except json.JSONDecodeError as exc:
                        raise ControlPlaneError(
                            502, "Control Plane returned invalid JSON"
                        ) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("error")
            except (json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise ControlPlaneError(
                exc.code,
                detail or f"Control Plane returned HTTP {exc.code}",
            ) from exc
        except ControlPlaneError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise ControlPlaneError(
                502, f"Sandbox Control Plane unavailable at {self.control_plane_url}: {exc}"
            ) from exc
        if "error" in result:
            error = result["error"]
            raise ControlPlaneError(
                502,
                f"MCP {error.get('code')}: {error.get('message')}",
            )
        tool_result = result.get("result") or {}
        structured = tool_result.get("structuredContent")
        if not isinstance(structured, dict):
            raise ControlPlaneError(502, "MCP response has no structuredContent")
        return structured

    @staticmethod
    def _interrupt_sse_response(response) -> None:
        """Interrupt a blocking urllib read without waiting on its I/O lock.

        ``HTTPResponse.close()`` may block behind ``BufferedReader.readline()``
        in another thread. Shutting down the underlying socket first wakes the
        reader, after which the normal close path is prompt and deterministic.
        Test doubles and alternative response wrappers still get the fallback
        ``close()`` call when no socket is exposed.
        """
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        response.close()

    @staticmethod
    def _read_sse_stream(
        response,
        request_id: str,
        on_chunk=None,
        is_cancelled=None,
    ) -> dict:
        final = None
        data_lines: list[str] = []
        lines: queue.Queue[object] = queue.Queue(maxsize=64)
        stopped = threading.Event()
        eof = object()

        def publish(item: object) -> None:
            while not stopped.is_set():
                try:
                    lines.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def read_lines() -> None:
            try:
                while not stopped.is_set():
                    raw_line = response.readline()
                    publish(raw_line)
                    if not raw_line:
                        return
            except BaseException as error:
                publish(error)
            finally:
                publish(eof)

        reader = threading.Thread(
            target=read_lines,
            name="sandbox-sse-reader",
            daemon=True,
        )
        reader.start()

        def dispatch() -> None:
            nonlocal final
            if not data_lines:
                return
            try:
                message = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                return
            if message.get("id") == request_id:
                final = message
                return
            if message.get("method") != "notifications/progress":
                return
            params = message.get("params") or {}
            meta = params.get("_meta") or {}
            channel = meta.get("channel")
            sequence = meta.get("sequence")
            chunk = params.get("message")
            if (
                params.get("progressToken") == request_id
                and channel in {"stdout", "stderr"}
                and isinstance(sequence, int)
                and not isinstance(sequence, bool)
                and sequence >= 1
                and isinstance(chunk, str)
                and chunk
                and callable(on_chunk)
            ):
                on_chunk(channel, chunk, sequence)

        try:
            while True:
                if callable(is_cancelled) and is_cancelled():
                    stopped.set()
                    SandboxManager._interrupt_sse_response(response)
                    raise ControlPlaneError(499, "Sandbox command cancelled")
                try:
                    item = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is eof:
                    dispatch()
                    break
                if isinstance(item, BaseException):
                    raise ControlPlaneError(
                        502,
                        f"Sandbox SSE read failed: {item}",
                    ) from item
                raw_line = item
                if not isinstance(raw_line, bytes):
                    raise ControlPlaneError(502, "Sandbox SSE returned a non-byte line")
                if not raw_line:
                    dispatch()
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    dispatch()
                    data_lines.clear()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        finally:
            stopped.set()
            reader.join(timeout=0.5)
            if reader.is_alive():
                #The reader is blocked on response.readline(), and stopped cannot wake it up——
                #It is waiting for the socket, not this Event. Use the same method to cancel the path and read it first
                #Interrupt and wait for it to close. If you don't do this, the thread will hang on the urlopen socket.
                #Discharge after timeout (maximum timeout_seconds + 15), and when the with block exits
                #response.close() would get stuck behind its I/O lock - leaking a thread would become
                #Blocks the caller for the same amount of time.
                SandboxManager._interrupt_sse_response(response)
                reader.join(timeout=_READER_JOIN_SECONDS)
                if reader.is_alive():
                    #The reading has been interrupted and the stall is still not closed. It can only be that the response substitute does not exist at all.
                    #The underlying socket. The thread is left here and cannot be processed anymore, but it must leave traces.
                    global _SSE_READER_LEAKS
                    _SSE_READER_LEAKS += 1
        if final is None:
            raise ControlPlaneError(502, "MCP SSE ended without a final response")
        return final

    @staticmethod
    def _parse_sse_result(raw: str, request_id: str) -> dict:
        final = None
        for block in raw.replace("\r\n", "\n").split("\n\n"):
            data_lines = [
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                message = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                final = message
        if final is None:
            raise ControlPlaneError(502, "MCP SSE ended without a final response")
        return final

    def status(self, session_key: str | None = None) -> dict:
        with self._lock:
            lease = self._lease(session_key)
            return {
                "session_id": lease.session_id,
                "workspace_id": lease.workspace_id,
                "workspace_ready": bool(lease.workspace_id),
                "sandbox_id": lease.sandbox_id,
                "runtime_ready": bool(lease.sandbox_id),
                "template": lease.sandbox_template,
            }

    def release_runtime(self, session_key: str | None = None) -> dict:
        with self._lock:
            key = session_key or current_session_key()
            lease = self._leases.get(key)
            if lease is None or lease.sandbox_id is None:
                return {"released": False, "reason": "no active Runtime"}
            sandbox_id = lease.sandbox_id
            result, _ = self._request(
                "DELETE",
                f"/v1/sandboxes/{sandbox_id}",
            )
            lease.sandbox_id = None
            lease.sandbox_token = None
            lease.sandbox_token_expires_at = 0
            lease.sandbox_checked_at = 0
            lease.sandbox_template = None
            return result


MANAGER = SandboxManager()


class Sandbox:
    """Named facade over the Workspace/Runtime model.

    A name identifies a persistent Workspace. Stopping the Sandbox releases its
    current Runtime while preserving Workspace files.
    """

    def __init__(
        self,
        name: str,
        *,
        manager: SandboxManager | None = None,
        template: str | None = None,
    ):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("sandbox name must not be empty")
        if len(name) > 128:
            raise ValueError("sandbox name must be at most 128 characters")
        self.name = name
        self._manager = manager or MANAGER
        self._template = template

    @classmethod
    def create(
        cls,
        name: str | None = None,
        *,
        template: str | None = None,
        manager: SandboxManager | None = None,
    ) -> "Sandbox":
        sandbox = cls(
            name or f"sandbox-{uuid.uuid4().hex[:12]}",
            manager=manager,
            template=template,
        )
        try:
            lease = sandbox._manager.lookup_runtime(sandbox.name)
        except ControlPlaneError as exc:
            if exc.status != 404:
                raise
            sandbox._manager.ensure_runtime(sandbox.name, template=template)
        else:
            if template is not None and lease.sandbox_template != template:
                raise ControlPlaneError(
                    409,
                    f"sandbox already running with template "
                    f"{lease.sandbox_template!r}; stop it before "
                    f"switching to {template!r}",
                )
        return sandbox

    @classmethod
    def get(
        cls,
        name: str,
        *,
        resume: bool = False,
        manager: SandboxManager | None = None,
    ) -> "Sandbox":
        sandbox = cls(name, manager=manager)
        if resume:
            try:
                sandbox._manager.lookup_runtime(name)
            except ControlPlaneError as exc:
                if exc.status != 404:
                    raise
                sandbox._manager.ensure_runtime(name)
        else:
            sandbox._manager.lookup_runtime(name)
        return sandbox

    @classmethod
    def get_or_create(
        cls,
        name: str,
        *,
        template: str | None = None,
        manager: SandboxManager | None = None,
    ) -> "Sandbox":
        return cls.create(name, template=template, manager=manager)

    @property
    def sandbox_id(self) -> str | None:
        return self._manager.status(self.name)["sandbox_id"]

    @property
    def workspace_id(self) -> str | None:
        return self._manager.status(self.name)["workspace_id"]

    def status(self) -> dict:
        return self._manager.status(self.name)

    def run_command(
        self,
        command: str,
        args: list[str] | tuple[str, ...] | None = None,
        *,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        if not isinstance(command, str) or not command:
            raise ValueError("command must not be empty")
        argv = [command, *(args or [])]
        if not all(isinstance(value, str) for value in argv):
            raise TypeError("command arguments must be strings")
        with session_context(self.name):
            result = self._manager.shell(
                shlex.join(argv),
                timeout_seconds=timeout_seconds,
            )
        return CommandResult(
            exit_code=int(result.get("exit_code", -1)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            timed_out=bool(result.get("timed_out", False)),
            output_truncated=bool(result.get("output_truncated", False)),
        )

    def read_file(self, path: str) -> dict:
        with session_context(self.name):
            return self._manager.read_file(path)

    def write_file(self, path: str, content: str) -> dict:
        with session_context(self.name):
            return self._manager.write_file(path, content)

    def write_files(self, files: list[dict[str, str]]) -> list[dict]:
        results = []
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "content"}:
                raise ValueError("each file must contain exactly path and content")
            results.append(self.write_file(file["path"], file["content"]))
        return results

    def stop(self) -> dict:
        if self.sandbox_id is None:
            self._manager.lookup_runtime(self.name)
        return self._manager.release_runtime(self.name)

    def checkpoint(self) -> dict:
        """Create a Workspace checkpoint archive."""
        return self._manager.checkpoint_workspace(self.name)


def release_runtime(session_key: str | None = None) -> dict:
    return MANAGER.release_runtime(session_key)
