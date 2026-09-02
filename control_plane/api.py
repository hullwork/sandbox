#!/usr/bin/env python3
"""HTTP adapter for authentication, request parsing, and responses.

The adapter depends on the ``core`` domain module. ``server.py`` is the composition
root and starts this adapter, so the dependency graph remains acyclic.
"""
from __future__ import annotations

from typing import Any
from .store import (
    GLOBAL_TENANT,
    WORKSPACE_AT_CAPACITY,
    ApiKey,
    StoreError,
)
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from http import HTTPStatus
from .kube import KubeError
from urllib.request import Request
import contextlib
import hashlib
import hmac
import json
from . import metrics as metrics_lib
from . import grafana_proxy
from urllib.parse import parse_qs, urlparse
import re
import secrets
import select
import tempfile
import time

from . import core as control_plane
from . import oidc, session, tracing

__all__ = (
    "ApiHandler",
)


def query_params(parsed) -> dict[str, list[str]]:
    from urllib.parse import parse_qs

    return parse_qs(parsed.query, keep_blank_values=True)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "sandbox-control-plane/0.7.0"
    # socketserver applies this with socket.settimeout(); a client that never
    # finishes its request line, or a half-open connection, otherwise pins a
    # thread forever (file_service.ApiHandler carries the same bound).
    timeout = 30
    #The claims verified by require_scoped_auth for this request (including owner binding)
    scoped_claims: dict | None = None
    #: Does this request come in with scoped token? Separate from scoped_claims because of read-only access
    #: (require_workspace_read_auth) also recognizes scoped tokens, but claims should not be shared
    #: object routing; What the audit side wants is "whether this is a scoped request."
    scoped_credential: bool = False
    #: Trace id of the request being served. Assigned in parse_request so that
    #: every request has one before any branch runs, including the ones that
    #: never reach a route.
    trace_id: str = ""
    request_span: tracing.Span | None = None
    response_status: int = 500
    request_started: float = 0.0

    @staticmethod
    def metric_route(path: str) -> str:
        """Bounded route label; identifiers and arbitrary suffixes never escape."""
        if path in {"/livez", "/readyz", "/healthz", "/metrics"}:
            return path
        if path.startswith(grafana_proxy.ROUTE_PREFIX):
            return "/grafana/*"
        patterns = (
            (r"/v1/sandboxes/[^/]+/mcp", "/v1/sandboxes/{id}/mcp"),
            (r"/v1/sandboxes/[^/]+/token", "/v1/sandboxes/{id}/token"),
            (r"/v1/sandboxes/[^/]+", "/v1/sandboxes/{id}"),
            (r"/v1/workspaces/[^/]+/checkpoints/[^/]+", "/v1/workspaces/{id}/checkpoints/{id}"),
            (r"/v1/workspaces/[^/]+/checkpoints", "/v1/workspaces/{id}/checkpoints"),
            (r"/v1/workspaces/[^/]+", "/v1/workspaces/{id}"),
            (r"/v1/admin/(?:keys|tenants|templates)/[^/]+", "/v1/admin/{resource}/{id}"),
            (r"/v1/admin/[^/]+", "/v1/admin/{resource}"),
            (r"/v1/storage/[^/]+", "/v1/storage/{operation}"),
            (r"/v1/auth/[^/]+", "/v1/auth/{operation}"),
            (r"/v1/(?:sandboxes|workspaces|whoami|monitoring)", path),
        )
        for pattern, label in patterns:
            if re.fullmatch(pattern, path):
                return label
        return "/unmatched"

    def handle_one_request(self) -> None:
        self.command = ""
        self.trace_id = ""
        self.request_started = time.monotonic()
        self.response_status = 500
        self.request_span = None
        try:
            super().handle_one_request()
        finally:
            if getattr(self, "command", ""):
                route = self.metric_route(urlparse(getattr(self, "path", "")).path)
                method = self.command if self.command in {"GET", "POST", "PUT", "DELETE"} else "OTHER"
                status_class = f"{int(self.response_status) // 100}xx"
                elapsed = max(0.0, time.monotonic() - self.request_started)
                control_plane.HTTP_REQUESTS.inc(
                    route=route, method=method, status_class=status_class
                )
                control_plane.HTTP_REQUEST_SECONDS.observe(
                    elapsed, route=route, method=method
                )
                if self.request_span is not None:
                    self.request_span.set_attribute("http.route", route)
                    self.request_span.set_attribute("http.response.status_code", int(self.response_status))
                    self.request_span.end(
                        error=RuntimeError("http error") if int(self.response_status) >= 500 else None
                    )

    def parse_request(self) -> bool:
        """Per-request setup that must not depend on remembering to call it.

        🔴 The chokepoint is here rather than at the top of each ``do_*``: one
        handler instance serves several requests under keep-alive, so state has
        to be re-established per request, and four dispatchers are four places
        to forget. Everything downstream reads ``current_trace_id``, so a miss
        would not fail loudly - it would quietly emit an unrelated trace.
        """
        parsed = super().parse_request()
        if parsed:
            self.trace_id, flags, parent_span_id = tracing.inbound_context(self.headers)
            tracing.set_current(self.trace_id, flags, parent_span_id)
            self.request_span = tracing.start_span(
                "http.request",
                kind=2,
                attributes={
                    "http.request.method": self.command,
                    "url.path": urlparse(self.path).path,
                },
            )
        return parsed

    def current_trace_id(self) -> str:
        """The trace id, generating one if the request never got parsed.

        A request line so malformed that parsing failed still gets an error
        response, and that response still has to be traceable. Cached so the log
        line and the response header cannot disagree.
        """
        if not self.trace_id:
            self.trace_id = tracing.new_trace_id()
            tracing.set_current(self.trace_id)
        return self.trace_id

    def send_response(self, code, message=None):
        self.response_status = int(code)
        super().send_response(code, message)
        # Echoed so a caller can quote it in a bug report, and so a browser
        # devtools pane shows the same id the server logged.
        self.send_header(tracing.REQUEST_ID_HEADER, self.current_trace_id())

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"{self.address_string()} - {fmt % args} "
            f"trace_id={self.current_trace_id()}",
            flush=True,
        )

    def send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status: int, payload: object) -> None:
        self.send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if length < 0 or length > control_plane.MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def bearer_token(self) -> str:
        return control_plane.parse_bearer_token(self.headers.get("Authorization", ""))

    #: Which tenant this request represents. None indicates the global view of the management plane (all tenants can be seen).
    #: One handler instance per request, not shared.
    tenant_id: str | None = None
    api_key: ApiKey | None = None
    #: Whether this request has been acknowledged once. See item 2 of require_control_plane_auth 🔴.
    authenticated: bool = False
    #: Claims of the Console browser session that authenticated this request.
    session_claims: dict | None = None
    #: The pseudonymous subject this request acts for, from X-Acting-Subject.
    #: 🔴 A pseudonym inside the credential's tenant, never a tenant selector:
    #: which tenant a request belongs to is decided by the credential alone.
    acting_subject: str | None = None

    def require_control_plane_auth(self) -> bool:
        """Authentication and determining who the request represents. **No matter how many times you acknowledge it in a request, it will only count once. **

        Four types of credentials, with priority from top to bottom:
          1. Console browser session - minted only by the OIDC callback; carries the role the
             identity provider's claims mapped to.
          2. Static SANDBOX_CONTROL_PLANE_TOKEN - **break-glass** only. Off by default wherever an OIDC provider
             is configured, audited on every use, and never a signing key (see control_plane/session.py).
          3. Management plane key (issued for the admin scope) - can bring X-Sandbox-Tenant to represent any tenant;
             Without it, it is a global view.
          4. Tenant key (issued for a tenant scope) - the tenant is fixed, nothing written in the header will count.

        When STORE is not configured, only item 2 is available, and the behavior is exactly the same as before multi-tenancy was introduced.

        🔴 Idempotence is not optional: the same request will indeed be acknowledged twice - do_DELETE before route distribution
           Acknowledge it unconditionally first, and then follow the DELETE /v1/storage/objects route
           back to here a second time. The second time will
           CREDENTIAL_USES and STORE.touch_api_key are recorded a second time each. CREDENTIAL_USES
           It is exactly the exit criteria for "whether anyone is still using the legacy static token": double counting will not affect it, right?
           Zero", but it will double the usage on this road out of thin air, so the trend of "consumption is declining" cannot be read.
           Incidentally, it is also more correct - the identity of the same request should not be re-determined midway."""
        if self.authenticated:
            return True
        self.authenticated = self._authenticate()
        return self.authenticated

    def _authenticate(self) -> bool:
        """The actual determination of require_control_plane_auth. Return False when a response has been sent."""
        claims = session.read(
            control_plane.SESSION_SECRET,
            self.headers,
            secure=control_plane.CONSOLE_COOKIES_SECURE,
            method=getattr(self, "command", "GET"),
        )
        if claims is not None:
            self.session_claims = claims
            control_plane.CREDENTIAL_USES.inc(kind="console")
            if not self.reject_unauthorized_acting_subject():
                return False
            if claims.get("kind") == "admin":
                return self._assume_admin()
            if not self.reject_tenant_selection():
                return False
            return self._assume_tenant(claims.get("tenant"), None)
        token = self.bearer_token()
        if not token:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        # 🔴 SANDBOX_CONTROL_PLANE_TOKEN is empty unless SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED resolved to
        # true (control_plane._local_login_enabled). That is the single authority for
        # whether the break-glass path exists: switching it off removes the
        # credential from the process rather than adding a branch that declines
        # it, so the route cannot be reached by going around the Console.
        if control_plane.SANDBOX_CONTROL_PLANE_TOKEN and hmac.compare_digest(token, control_plane.SANDBOX_CONTROL_PLANE_TOKEN):
            control_plane.CREDENTIAL_USES.inc(kind="break-glass")
            self.log_break_glass_use()
            if not self.reject_unauthorized_acting_subject():
                return False
            return self._assume_admin()

        if control_plane.STORE is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        try:
            key = control_plane.STORE.authenticate(token)
        except StoreError as exc:
            # Do not fail open when the store is unavailable. Report 503 rather than 401: a 401 makes the caller
            # think its credentials are wrong and rotate them, when the right response is to wait for the store to recover.
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"control plane store unavailable: {exc}"},
            )
            return False
        if key is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False

        self.api_key = key
        control_plane.CREDENTIAL_USES.inc(kind="admin" if key.is_admin else "tenant")
        if not self.bind_acting_subject(key):
            return False
        if key.is_admin:
            return self._assume_admin(key)
        if not self.reject_tenant_selection():
            return False
        return self._assume_tenant(key.tenant_id, key)

    def bind_acting_subject(self, key: ApiKey) -> bool:
        """Record which pseudonymous subject this credential is acting for.

        🔴 Refused, not ignored, when the key does not carry the permission. An
        ignored header means the caller believes it wrote on someone's behalf
        while the platform filed the work under the credential itself - the two
        sides then disagree about who owns the data, and nothing reports it.
        This is Kubernetes impersonation semantics: whether an identity may act
        for another is a property of that identity, not a global setting."""
        raw = (self.headers.get("X-Acting-Subject") or "").strip()
        if not raw:
            return True
        if not key.may_act_as_subjects:
            self.log_acting_subject(raw, outcome="deny")
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "this credential may not act for a subject"},
            )
            return False
        if not control_plane.ACTING_SUBJECT_RE.fullmatch(raw):
            self.log_acting_subject(raw, outcome="deny")
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "X-Acting-Subject must be 32 lowercase hex characters"},
            )
            return False
        self.acting_subject = raw
        self.log_acting_subject(raw, outcome="allow")
        return True

    def reject_tenant_selection(self) -> bool:
        """A tenant-bound credential may not name a tenant. Ever.

        🔴 Refused **even when the value matches the credential's own tenant**.
        Accepting the matching case looks harmless and is the whole problem: a
        caller that habitually sends the header would be served correctly for
        as long as the two agreed, learning that the header is honoured, and
        would find out otherwise only on the request where they differ - by
        which time it believes it wrote somewhere it did not. Refusing every
        one of them means the caller learns the truth on its first request
        rather than on its first cross-tenant one.

        🔴 Refused, not ignored, for the same reason. An ignored header answers
        `200` and files the work under the credential's tenant while the caller
        records it under the one it named; the two sides then disagree about
        who owns the data, and nothing reports it. That shape - a guard that
        does nothing and looks exactly like a guard that worked - is the one
        this project keeps having to dig back out.

        The management plane is a different identity, not an exception: an
        admin key carries no tenant of its own, so naming one is the only way
        it can act for a tenant at all.
        """
        requested = (self.headers.get("X-Sandbox-Tenant") or "").strip()
        if not requested:
            return True
        print(
            f"auth key={self.api_key.id if self.api_key else '-'} "
            f"tenant_selection={requested[:64]} "
            f"route={getattr(self, 'command', '?')} {urlparse(self.path).path} "
            f"outcome=deny",
            flush=True,
        )
        self.send_json(
            HTTPStatus.FORBIDDEN,
            {
                "error": "this credential is bound to a tenant; "
                "X-Sandbox-Tenant is not accepted"
            },
        )
        return False

    def reject_unauthorized_acting_subject(self) -> bool:
        """Credentials that are not API keys can never act for a subject.

        The break-glass token and a Console session both authenticate a person,
        and neither carries the permission bit an API key can be issued with -
        so the header is refused rather than silently dropped, for the same
        reason as in bind_acting_subject."""
        raw = (self.headers.get("X-Acting-Subject") or "").strip()
        if not raw:
            return True
        self.log_acting_subject(raw, outcome="deny")
        self.send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "this credential may not act for a subject"},
        )
        return False

    def log_acting_subject(self, subject: str, *, outcome: str) -> None:
        """One line per impersonating call, per the cross-service contract."""
        print(
            f"auth key={self.api_key.id if self.api_key else '-'} "
            f"acting_as={subject[:32]} "
            f"route={getattr(self, 'command', '?')} {urlparse(self.path).path} "
            f"outcome={outcome}",
            flush=True,
        )

    def log_break_glass_use(self) -> None:
        """Every use of the static admin token, with where it came from.

        Written down here **and** in the README. An escape hatch that exists
        only in the source is one the operators do not know about and an
        attacker reading the code does."""
        print(
            f"auth break-glass SANDBOX_CONTROL_PLANE_TOKEN source={self.address_string()} "
            f"forwarded-for={self.headers.get('X-Forwarded-For', '-')} "
            f"route={getattr(self, 'command', '?')} {urlparse(self.path).path}",
            flush=True,
        )

    def _assume_admin(self, key: ApiKey | None = None) -> bool:
        """Management identity. X-Sandbox-Tenant represents the tenant when it is present, and the global view when it is not present."""
        requested = (self.headers.get("X-Sandbox-Tenant") or "").strip()
        if not requested:
            self.tenant_id = None
            return True
        return self._assume_tenant(requested, key)

    def _assume_tenant(self, tenant_id: str | None, key: ApiKey | None) -> bool:
        if not tenant_id:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        if control_plane.STORE is None:
            #Without storage, there is no concept of tenants; specifying a tenant is a configuration error, and it should be stated clearly rather than silently.
            #Single tenant processing.
            self.send_json(
                HTTPStatus.CONFLICT,
                {"error": "tenants require SANDBOX_STORE_BACKEND to be configured"},
            )
            return False
        try:
            tenant = control_plane.STORE.get_tenant(tenant_id)
        except StoreError as exc:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"control plane store unavailable: {exc}"},
            )
            return False
        if tenant is None:
            self.send_json(
                HTTPStatus.NOT_FOUND, {"error": f"unknown tenant: {tenant_id}"}
            )
            return False
        if not tenant.active:
            #Deactivated tenants can still be seen by the management interface, but can no longer be operated - deactivation is to stop losses.
            #Releasing the write operation is meaningless.
            self.send_json(
                HTTPStatus.FORBIDDEN, {"error": f"tenant is suspended: {tenant_id}"}
            )
            return False
        self.tenant_id = tenant_id
        if key is not None:
            with contextlib.suppress(StoreError):
                control_plane.STORE.touch_api_key(key.id)
        return True

    def precheck_workspace_quota(self, workspace_id: str) -> bool:
        """The **fast path** of per-tenant quotas is not a criterion. The criterion is admit_workspace_ownership.

        🔴 The count read here is not in the same transaction as the subsequent write, so it will naturally leak -
           Both concurrent requests can read max-1 and pass. Save it for one thing only: obviously over quota
           Immediately return 429, saving a volume round trip, and not leaving a volume on the volume to rely on reaper later.
           Empty directories collected. What really stops over-issuance is the conditional insertion at the registration step.
           When changing the quota semantics, both parts must be changed, but only if one part is wrong, the quota will be exceeded.

        Both layers are required: the global layer protects volumes (don’t fill up the disk), and this layer protects tenants from interacting with each other.
        squeeze. When there is only a global quota, one tenant can legally take up all the quota."""
        if control_plane.STORE is None or self.tenant_id is None:
            return True
        try:
            tenant = control_plane.STORE.get_tenant(self.tenant_id)
            if tenant is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"unknown tenant: {self.tenant_id}"},
                )
                return False
            # Idempotent re-entry does not consume new quota - it would be strange to be unable to use an existing Workspace once the quota is full.
            if control_plane.STORE.owner_of(workspace_id) == self.tenant_id:
                return True
            used = control_plane.STORE.count_workspaces(self.tenant_id)
        except StoreError as exc:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"control plane store unavailable: {exc}"},
            )
            return False
        if used >= tenant.max_workspaces:
            control_plane.QUOTA_REJECTIONS.inc(gate="tenant_workspace")
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": (
                        f"tenant workspace capacity reached "
                        f"({used}/{tenant.max_workspaces})"
                    )
                },
            )
            return False
        return True

    MANAGEMENT_TENANT = "management"

    def ensure_management_tenant(self) -> str:
        """The reserved tenant the unscoped management identity is filed under.

        It has to exist before anything looks a limit up or mints a ticket,
        because capability_epoch refuses to answer for a subject with no row --
        minting for an unknown subject is the forgery that epoch exists to stop.

        🔴 Created on demand rather than seeded by ensure_schema: an operator
        who upgrades an existing deployment never re-runs seeding, so seeding
        would make this work on fresh installs and fail on exactly the
        deployments that already have data. Creating it at the point of use has
        no such split.

        The create is best-effort against a concurrent replica doing the same
        thing: a losing insert is fine as long as the row is there afterwards.
        """
        if control_plane.STORE.get_tenant(self.MANAGEMENT_TENANT) is None:
            try:
                control_plane.STORE.create_tenant(
                    self.MANAGEMENT_TENANT,
                    "Reserved management-plane identity",
                    max_workspaces=1024,
                    max_runtimes=1024,
                )
            except Exception:
                if control_plane.STORE.get_tenant(self.MANAGEMENT_TENANT) is None:
                    raise
        return self.MANAGEMENT_TENANT

    def admit_workspace_ownership(
        self,
        workspace_id: str,
        *,
        principal_kind: str,
        principal_id: str,
        session_key: str,
    ) -> bool:
        """Occupy the quota + register for ownership, and the transaction is completed in one go. This is the criterion for per-tenant quota.

        🔴 It turns out that this step is an unconditional INSERT, and the quota is determined in the count four transactions before ——
           Two requests with different session_ids will be released after each count reaches max-1, with one interval in between.
           The volume round trip takes tens to hundreds of milliseconds, and the window is so large that a single copy can be stably over-issued.
           Now counting and inserting are combined into one statement, and row 0 means "the concurrent party has taken up the last quota".

        Returning False indicates that a response has been sent, and the caller returns directly."""
        if control_plane.STORE is None:
            # No control-plane store configured: no per-tenant quota to evaluate.
            return True
        tenant_id = self.tenant_id
        principal_kind = principal_kind or "service"
        principal_id = principal_id or "break-glass-control-plane-token"
        if tenant_id is None:
            # The management identity keeps the pre-store single-tenant
            # semantics, but capability_epoch still requires a workspace row.
            # Register these workspaces under a reserved management tenant so
            # runtime tickets remain revocable instead of failing on the first
            # POST /v1/sandboxes.
            tenant_id = self.ensure_management_tenant()
        try:
            tenant = control_plane.STORE.get_tenant(tenant_id)
        except StoreError as exc:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"control plane store unavailable: {exc}"},
            )
            return False
        if tenant is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": f"unknown tenant: {self.tenant_id}"},
            )
            return False
        try:
            outcome = control_plane.STORE.admit_workspace(
                tenant_id,
                workspace_id,
                principal_kind=principal_kind,
                principal_id=principal_id,
                session_key=session_key,
                limit=tenant.max_workspaces,
            )
        except StoreError as exc:
            # Ownership conflicts (this id already belongs to another tenant) and store outages both land here. Neither may
            # be reported as "quota full": that would present a data inconsistency as a capacity problem.
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"failed to record ownership: {exc}"},
            )
            return False
        if outcome == WORKSPACE_AT_CAPACITY:
            control_plane.QUOTA_REJECTIONS.inc(gate="tenant_workspace")
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": (
                        f"tenant workspace capacity reached "
                        f"({tenant.max_workspaces}/{tenant.max_workspaces})"
                    )
                },
            )
            return False
        # Only two outcomes remain here: ADMITTED (new) and REUSED (idempotent re-entry), both successful -
        # the directory exists and the ownership row is recorded.
        # 🔴 A rejected, newly created directory is **not** deleted: it may be a soft-deleted directory of an old
        # Workspace whose data is still there, and deleting it would lose user data. Let the reaper collect it by idle TTL.
        return True

    def tenant_runtime_limit(self) -> int | None:
        """The upper limit of runtimes for this tenant; None = no limit (no store configured, or the global management-plane identity).

        The division of labor is different from that of admit_workspace_ownership: here we only take the upper limit and do not judge or exceed it.
        The judgment is placed in the access lock of ensure_runtime and is done together with the global layer - the count of Runtime
        If you use K8s label selector instead of SQL, lock outsourcing will be leaked during concurrent creation.
        Neither a store outage (StoreError) nor an unknown tenant (KubeError 404) sends a response here.
        Leave it to the unified exit at the end of do_POST: the caller also needs to use the return value to continue the creation process."""
        if control_plane.STORE is None or self.tenant_id is None:
            if control_plane.STORE is None:
                return None
            #Same reserved tenant as the ownership path. Without this the
            #management identity gets "unknown tenant: management" on its first
            #POST /v1/sandboxes whenever no management workspace happens to
            #have been registered yet -- which reads as a misconfiguration
            #rather than as the row simply not existing yet.
            tenant_id = self.ensure_management_tenant()
        else:
            tenant_id = self.tenant_id
        tenant = control_plane.STORE.get_tenant(tenant_id)
        if tenant is None:
            raise KubeError(
                HTTPStatus.NOT_FOUND, f"unknown tenant: {tenant_id}"
            )
        return tenant.max_runtimes

    def require_admin(self) -> bool:
        """Management plane operations: create tenants and issue keys.

        🔴 With the management interface key of X-Sandbox-Tenant, it is not counted as the management interface identity. The semantics of that head
        It is "acting as the tenant". Allowing it to create tenants is equivalent to allowing the tenant credentials to create new tenants."""
        if not self.require_control_plane_auth():
            return False
        if self.tenant_id is not None:
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "admin operations require an admin key without "
                    "X-Sandbox-Tenant"
                },
            )
            return False
        if self.api_key is not None and not self.api_key.is_admin:
            self.send_json(
                HTTPStatus.FORBIDDEN, {"error": "admin key required"}
            )
            return False
        if control_plane.STORE is None:
            self.send_json(
                HTTPStatus.CONFLICT,
                {"error": "tenants require SANDBOX_STORE_BACKEND to be configured"},
            )
            return False
        return True

    def require_console_admin(self) -> bool:
        """Administrator gate for the read-only observability panel.

        Deliberately not ``require_admin``: that one additionally demands a
        configured control-plane store, because every route behind it writes
        tenants or keys.  This route writes nothing - it renders a dashboard -
        and a single-tenant deployment with no store has administrators too.
        Reusing ``require_admin`` there would answer 409 for a panel, which
        reads as "the panel is broken" rather than "you are not an admin".

        The two checks that do carry over are the ones that decide *who* an
        administrator is: a management-plane credential that arrived carrying
        ``X-Sandbox-Tenant`` is acting as a tenant and is not one, and a tenant
        key never is.
        """
        if not self.require_control_plane_auth():
            return False
        if self.tenant_id is not None or (
            self.api_key is not None and not self.api_key.is_admin
        ):
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "the observability panel requires an admin credential"},
            )
            return False
        return True

    def send_panel_bytes(
        self, status: int, payload: bytes, content_type: str, extra: dict
    ) -> None:
        """Write a proxied panel response with its own framing policy.

        🔴 ``frame-ancestors 'self'`` and ``X-Frame-Options: SAMEORIGIN``, not
        the Console's ``'none'``/``DENY``: this response *is* the framed
        document, and the Console's own policy would make the browser refuse to
        render the Console's own iframe.  The override is scoped to this one
        path; every other response keeps the stricter policy.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Security-Policy", grafana_proxy.PANEL_CSP)
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def handle_grafana_panel(self, path: str, query: str) -> None:
        """Serve one embedded Grafana panel on the Console's own origin.

        🔴 This prefix is deliberately outside the three-way route contract
        (``control_plane.ROUTE_AUTH`` / OpenAPI / dispatch, see
        ``tests/test_route_completeness.py``).  It is not a Control Plane API: it is a
        pass-through into a third party's URL space, and enumerating Grafana's
        asset tree as Control Plane routes would be both wrong and unmaintainable.
        Its contract is ``grafana_proxy.ALLOWED``, and
        ``tests/test_grafana_embed.py`` holds it to the same standard the route
        table holds everything else to - including the unauthenticated-401 case
        that ROUTE_AUTH would otherwise have covered.

        Order: administrator, then configuration, then allowlist.  Checking
        configuration first would let any signed-in user learn whether Grafana
        is wired up by comparing 403 with 404.
        """
        if not self.require_console_admin():
            return
        config = grafana_proxy.load_config()
        if not config.enabled:
            # 404, not 503: with no Grafana configured this route does not
            # exist. The Console hides the tab for the same reason, and the
            # repository stays deployable with no Grafana anywhere.
            self.send_json(
                HTTPStatus.NOT_FOUND, {"error": "grafana is not configured"}
            )
            return
        upstream = grafana_proxy.upstream_path(path)
        if upstream is None or not grafana_proxy.is_allowed(self.command, upstream):
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "path is not part of the embedded panel"},
            )
            return
        if self.command != "GET" and not grafana_proxy.is_same_origin(
            self.headers, self.headers.get("Host", "")
        ):
            self.send_json(
                HTTPStatus.FORBIDDEN, {"error": "cross-origin request"}
            )
            return
        body = b""
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > grafana_proxy.MAX_REQUEST_BYTES:
                self.send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "request body is too large"},
                )
                return
            body = self.rfile.read(length) if length else b""
        if upstream == grafana_proxy.DS_QUERY_PATH and not (
            grafana_proxy.check_query_datasources(
                body, grafana_proxy.allowed_datasource_uids(config)
            )
        ):
            # 🔴 The allowlist bounds the URL; this bounds the body.
            # /api/ds/query dispatches on a datasource uid inside the request,
            # so URL-only filtering would leave "run anything against any
            # datasource this Grafana can reach" open. See
            # grafana_proxy.check_query_datasources.
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "query names a datasource this panel does not use"},
            )
            return
        try:
            status, headers, chunks = grafana_proxy.forward(
                config, self.command, upstream, query, self.headers, body
            )
        except grafana_proxy.ProxyError as exc:
            # The message is ours, never the upstream body: a Grafana error page
            # can name internal hosts and this response renders inside the
            # Console.
            print(f"grafana panel proxy failed: {exc}", flush=True)
            self.send_json(
                HTTPStatus.BAD_GATEWAY, {"error": "grafana is unreachable"}
            )
            return
        payload = b"".join(chunks)
        content_type = headers.pop("Content-Type", None) or "application/octet-stream"
        self.send_panel_bytes(status, payload, content_type, headers)

    #: Static SANDBOX_CONTROL_PLANE_TOKEN notation in the audit column (sandbox_templates.created_by).
    #:
    #: It has no row in the store and therefore no key_id. Recording NULL means "who wrote this" can never be answered -
    #: And this token is exactly the one that deserves the most attention: it has no expiry date yet. written as a fixed
    #: The sentinel value can be greped out and will not collide with the real key_id (16-digit hexadecimal).
    BREAK_GLASS_PRINCIPAL = "break-glass-control-plane-token"

    def send_object_store_busy(self, exc: Exception) -> None:
        """Queue full: 503 + retry available, not 400.

        Same reason as send_store_outage - ObjectStoreBusy is a RuntimeError
        subclass, falling into the trap at the end of do_* becomes 400, and the caller will change the request instead of retrying."""
        self.send_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": str(exc), "retry_after_seconds": 2},
        )

    def send_store_outage(self, exc: Exception) -> None:
        """Store unavailable: 503, not 400.

        🔴 StoreError is a subclass of RuntimeError, the one at the end of the three do_* methods
        `except (OSError, RuntimeError, ValueError)` will wrap it up as 400 - so
        "the store is unreachable" looks like "your request is wrong" to the caller, which then rotates the request and credentials.
        What you should really do is wait for the database to recover. The same judgment as in require_control_plane_auth."""
        control_plane.STORE_ERRORS.inc()
        self.send_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": f"control plane store unavailable: {exc}"},
        )

    def audit(
        self, action: str, *, target: str | None = None, outcome: str = "ok"
    ) -> None:
        """Drop an audit.

        🔴 Write failure **cannot** cause the business request to fail - that is equivalent to letting the availability of the audit table determine the service
           Availability. But it cannot be swallowed silently, otherwise the audit will be shut down without anyone knowing.
           Use indicators + logs to make the fact that "auditing is failing" itself observable."""
        if control_plane.STORE is None:
            return
        try:
            control_plane.STORE.record_audit(
                actor_kind=self.credential_kind(),
                actor_id=self.actor_id(),
                action=action,
                target=target,
                outcome=outcome,
                tenant_id=self.tenant_id,
            )
        except StoreError as exc:
            control_plane.AUDIT_FAILURES.inc()
            print(
                f"warning: audit write failed for {action}: {exc}", flush=True
            )

    def begin_oidc_login(self) -> None:
        """Start Authorization Code + PKCE against the deployment's provider."""
        if control_plane.OIDC_CONFIG is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no identity provider is configured"},
            )
            return
        try:
            location, envelope = oidc.begin(
                control_plane.OIDC_CONFIG, control_plane.SESSION_SECRET
            )
        except oidc.OidcError as exc:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header(
            "Set-Cookie",
            oidc.state_cookie(envelope, secure=control_plane.CONSOLE_COOKIES_SECURE),
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def complete_oidc_login(self, query: dict[str, list[str]]) -> None:
        """Exchange the code, verify the ID token, and mint a Console session."""
        if control_plane.OIDC_CONFIG is None:
            self.send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no identity provider is configured"},
            )
            return
        try:
            claims = oidc.complete(
                control_plane.OIDC_CONFIG,
                control_plane.SESSION_SECRET,
                query,
                self.headers,
                secure=control_plane.CONSOLE_COOKIES_SECURE,
            )
            kind, tenant_id = oidc.role_of(control_plane.OIDC_CONFIG, claims)
        except oidc.OidcError as exc:
            # One status for every way the flow can fail. Which check refused is
            # in the Control Plane log; telling the browser would help whoever is
            # probing the provider integration and nobody else.
            print(f"auth oidc rejected source={self.address_string()}: {exc}", flush=True)
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "sign-in was refused"})
            return
        if kind == "tenant":
            # 🔴 The tenant a claim names must already exist here. Creating one
            # on the strength of a login is how an identity provider becomes the
            # thing that provisions tenants in somebody else's control plane.
            if control_plane.STORE is None:
                self.send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "tenants require SANDBOX_STORE_BACKEND to be configured"},
                )
                return
            try:
                tenant = control_plane.STORE.get_tenant(str(tenant_id))
            except StoreError as exc:
                self.send_store_outage(exc)
                return
            if tenant is None or not tenant.active:
                print(
                    f"auth oidc rejected source={self.address_string()}: "
                    f"no active tenant {tenant_id}",
                    flush=True,
                )
                self.send_json(
                    HTTPStatus.FORBIDDEN, {"error": "sign-in was refused"}
                )
                return
        value, csrf, _ = session.issue(
            control_plane.SESSION_SECRET,
            kind=kind,
            tenant_id=tenant_id,
            subject=str(claims["sub"]),
            email=str(claims.get("email") or ""),
        )
        print(
            f"auth oidc accepted sub={claims['sub']} kind={kind} "
            f"tenant={tenant_id or '-'} source={self.address_string()}",
            flush=True,
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        for cookie in [
            oidc.state_cookie("", secure=control_plane.CONSOLE_COOKIES_SECURE),
            *session.set_cookies(
                value, csrf, secure=control_plane.CONSOLE_COOKIES_SECURE
            ),
        ]:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def key_options(payload: dict) -> tuple[list[str], int | None]:
        """Read the permissions and lifetime a key is being issued with.

        Both default to the narrow answer: no permissions, and - only because a
        control plane that cannot issue a long-lived operator key has no way to
        bootstrap itself - no expiry unless one is asked for. The store rejects
        an unknown permission string rather than storing something inert."""
        raw_permissions = payload.get("permissions", [])
        if not isinstance(raw_permissions, list) or not all(
            isinstance(item, str) for item in raw_permissions
        ):
            raise ValueError("permissions must be a list of strings")
        raw_expiry = payload.get("expires_in_seconds")
        if raw_expiry is None:
            return raw_permissions, None
        if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, int):
            raise ValueError("expires_in_seconds must be an integer")
        if not 0 < raw_expiry <= 365 * 24 * 3600:
            raise ValueError("expires_in_seconds must be 1..31536000")
        return raw_permissions, raw_expiry

    def actor_id(self) -> str | None:
        """Who to file this action under.

        A Console session has no key id, so the provider subject is recorded
        instead - prefixed, because the two namespaces are unrelated and a bare
        value would be indistinguishable from a key id in the audit table."""
        if self.api_key is not None:
            return self.api_key.id
        if self.session_claims is not None:
            return f"console:{self.session_claims.get('sub')}"
        return None

    def credential_kind(self) -> str:
        """What type of credentials were used for this request.

        🔴 break-glass must be individually distinguished: it is the static SANDBOX_CONTROL_PLANE_TOKEN, equivalent in power to
        an admin key but unrevocable, unattributable, and not tied to any person. Folded into the same kind as
        real admin keys, nobody could ever see who is still on that path and it would never be switched off.

        The criterion is that api_key is empty: require_control_plane_auth only sets it when it recognizes the key from the store
        Assignment, static token that path does not touch it.

        scoped must be judged before break-glass: scoped requests also do not carry api_key, if you miss it
        Every audit item that scoped_tenant_is_active falls into will be recorded as break-glass, which is exactly what happened above.
        (Who else is hanging on to static tokens) Confusing."""
        if self.session_claims is not None:
            return str(self.session_claims.get("kind"))
        if self.scoped_credential:
            return "scoped"
        if self.api_key is None:
            return "break-glass"
        return "admin" if self.api_key.is_admin else "tenant"

    def capabilities(self) -> list[str]:
        """What can this credential actually do under the identity of this request?

        Responsibility: Translate the existing criteria in the authentication branch into a list that can be directly used by the front end. Not introduced
             Any new authorization rules - each entry must correspond to an endpoint that will actually be allowed, otherwise the panel
             It will render a button that will give a 403 when clicked, which is worse than not rendering at all.
        Constraints: admin key with X-Sandbox-Tenant is not the admin identity (see
             require_admin's 🔴), so those items will not appear in this request.
             All management interface items require STORE to be configured: require_admin returns 409 on the spot when there is no store.
             To declare a capability that must fail is to lie."""
        caps = [
            "monitoring:read",
            "workspaces:read",
            "workspaces:write",
            "sandboxes:write",
            "templates:read",
        ]
        if self.tenant_id is not None:
            return sorted(caps)
        caps.append("workspaces:read:all")
        caps.append("nodes:read")
        if control_plane.STORE is None:
            return sorted(caps)
        caps += ["tenants:write", "keys:write", "templates:read:all"]
        if control_plane.SANDBOX_IMAGE_REGISTRIES:
            #The whitelist is not configured = template write fail closed, this item should not appear (Contract §2.4).
            caps.append("templates:write")
        return sorted(caps)

    def whoami_view(self) -> dict:
        """Cross-service contract: response body of `GET /v1/whoami`.

        The panel must first know "whose credential this is and what it can do" before it can decide which tabs to render. Without it,
        The front-end can only guess the identity by "try to call the management endpoint once, and 403 means it is a tenant" - which is equivalent to changing everyone's identity.
        Each load becomes an unauthorized detection.

        The tenant block only exists when it represents a tenant (tenant key, or management interface)
        X-Sandbox-Tenant), because only then can there be quotas."""
        view: dict[str, Any] = {
            "kind": self.credential_kind(),
            "tenant_id": self.tenant_id,
            "key_id": self.api_key.id if self.api_key else None,
            "acting_subject": self.acting_subject,
            "capabilities": self.capabilities(),
        }
        #Only an administrator is told whether an observability panel exists:
        #the tab is an operator view (the metrics behind it have no tenant
        #dimension), and telling a tenant that it is configured is a free fact
        #about the deployment that a tenant has no use for.
        if self.tenant_id is None and (
            self.api_key is None or self.api_key.is_admin
        ):
            view["grafana"] = grafana_proxy.capabilities(
                grafana_proxy.load_config()
            )
        if control_plane.STORE is None or self.tenant_id is None:
            return view
        tenant = control_plane.STORE.get_tenant(self.tenant_id)
        if tenant is not None:
            view["tenant"] = {
                "name": tenant.display_name,
                "status": tenant.status,
                "max_workspaces": tenant.max_workspaces,
                "used_workspaces": control_plane.STORE.count_workspaces(self.tenant_id),
            }
        return view

    def scope_workspaces(
        self, entries: list[dict]
    ) -> tuple[list[dict], dict[str, str], dict[str, str]]:
        """Converge the Workspaces listed on the volume to the range visible for this request.

        🔴 This is where the current overreach stops. The volume is a directory of all tenants. Without filtering, it is equal to any
        Tenant key can see other people's Workspace ID - and the ID is all subsequent operations based on the ID
        entrance.

        The management plane (tenant_id is None) looks at all, and also brings ownership; tenants only look at their own.
        When STORE is not configured, it degrades to single tenant and behaves the same as before.

        The third value is the store's last_used_at per Workspace - the clock the reaper actually runs
        (see workspace_view's recorded_last_used_at); it is returned for every visible Workspace."""
        if control_plane.STORE is None:
            return entries, {}, {}
        owned = control_plane.STORE.list_workspaces(self.tenant_id)
        owners = {row["workspace_id"]: row["tenant_id"] for row in owned}
        recorded = {
            row["workspace_id"]: row["last_used_at"]
            for row in owned
            if row.get("last_used_at")
        }
        if self.tenant_id is None:
            #Only global views return ownership. When representing a tenant, the column is always equal to itself.
            #It would be better not to give one extra column for nothing.
            return entries, owners, recorded
        # Directories without an ownership row in the store are invisible to tenants. They are either pre-multi-tenant stock
        #(the migration script will claim it), or it has been manually stuffed into the volume - neither of which should be visible to tenants.
        return [e for e in entries if e.get("id") in owners], {}, recorded

    def scope_sandboxes(self, runtimes: list[control_plane.RuntimeInstance]) -> list[dict]:
        """Runtimes converge according to their Workspace ownership.

        Runtime itself does not remember tenants; its ownership follows the Workspace. That way "changing ownership" touches
        one place only, and the Pod labels and the store rows can never disagree."""
        views = [control_plane.sandbox_view(runtime) for runtime in runtimes]
        if control_plane.STORE is None or self.tenant_id is None:
            return views
        owned = {
            row["workspace_id"] for row in control_plane.STORE.list_workspaces(self.tenant_id)
        }
        return [view for view in views if view.get("workspace_id") in owned]

    @staticmethod
    def _metrics_failure(exc: KubeError) -> dict:
        if exc.status == HTTPStatus.NOT_FOUND:
            reason = "metrics_api_unavailable"
        elif exc.status == HTTPStatus.FORBIDDEN:
            reason = "metrics_api_forbidden"
        else:
            reason = "metrics_api_error"
        return {"available": False, "reason": reason}

    def send_runtime_driver_error(
        self, exc: control_plane.RuntimeDriverError
    ) -> None:
        """Translate provider-neutral runtime failures at the HTTP boundary."""
        fallback = {
            control_plane.RuntimeDriverErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
            control_plane.RuntimeDriverErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
            control_plane.RuntimeDriverErrorCode.CONFLICT: HTTPStatus.CONFLICT,
            control_plane.RuntimeDriverErrorCode.CAPACITY: HTTPStatus.TOO_MANY_REQUESTS,
            control_plane.RuntimeDriverErrorCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
            control_plane.RuntimeDriverErrorCode.UNKNOWN: HTTPStatus.BAD_GATEWAY,
        }
        self.send_json(exc.status or fallback[exc.code], {"error": str(exc)})

    def monitoring_view(
        self,
        runtime_instances: list[control_plane.RuntimeInstance],
    ) -> dict:
        """Build one scoped monitoring snapshot from core and Metrics APIs.

        Node topology is cluster-wide and is therefore returned only for a global
        identity. Runtime rows reuse Workspace ownership filtering. Metrics API
        failures degrade actual usage to null while core status and declared
        requests/limits remain available.
        """
        visible = self.scope_sandboxes(runtime_instances)
        visible_ids = {item.get("id") for item in visible}
        visible_runtimes = [
            runtime
            for runtime in runtime_instances
            if runtime.runtime_id in visible_ids
        ]

        runtime_metrics: list[control_plane.RuntimeUsage] = []
        runtime_metric_state: dict = {"available": True, "reason": None}
        try:
            runtime_metrics = (
                control_plane.configured_runtime_driver()
                .list_runtime_metrics()
            )
        except control_plane.RuntimeDriverError as exc:
            runtime_metric_state = self._metrics_failure(exc)
        runtime_metric_by_name = {
            item.provider_id: item for item in runtime_metrics
        }

        nodes: list[dict] = []
        node_metric_state: dict | None = None
        nodes_visible = self.tenant_id is None
        if nodes_visible:
            core_nodes = control_plane.KUBE.list_cluster("nodes")
            node_metrics: list[dict] = []
            node_metric_state = {"available": True, "reason": None}
            try:
                node_metrics = control_plane.KUBE.list_cluster_group(
                    "metrics.k8s.io", "v1beta1", "nodes"
                )
            except KubeError as exc:
                node_metric_state = self._metrics_failure(exc)
            node_metric_by_name = {
                item.get("metadata", {}).get("name"): item
                for item in node_metrics
            }
            nodes = [
                control_plane.node_monitoring_view(
                    node, node_metric_by_name.get(node.get("metadata", {}).get("name"))
                )
                for node in core_nodes
            ]

        runtimes = [
            control_plane.runtime_monitoring_view(
                runtime,
                runtime_metric_by_name.get(runtime.provider_id),
            )
            for runtime in visible_runtimes
        ]
        if not nodes_visible:
            # A Pod's nodeName is cluster topology. Runtime ownership permits
            # resource monitoring, not discovery of shared infrastructure.
            for runtime in runtimes:
                runtime["node"] = None
        return {
            "scope": "cluster" if nodes_visible else "tenant",
            "nodes_visible": nodes_visible,
            "metrics": {
                "nodes": node_metric_state,
                "runtimes": runtime_metric_state,
            },
            "nodes": nodes,
            "runtimes": runtimes,
        }

    #: The <tenant>/<subject> partition this request's object operations run
    #: under, once resolve_object_owner has decided it. One handler instance per
    #: request, not shared.
    object_owner: str | None = None

    def resolve_object_owner(self, supplied: object, *, required: bool = True) -> bool:
        """Decide the ``<tenant>/<subject>`` partition an object operation writes under.

        Every object key is ``users/<tenant>/<subject>/...``, so this value is
        the whole of object-storage isolation, and unlike a request header it is
        **persisted**: it is the prefix the bytes live under for as long as they
        exist.

        🔴 The tenant segment is derived here, from the credential, and is never
        read from the request. It used to be read from the request, and that is
        the reason this whole route group was reachable by the management plane
        only: a caller that spells out its own partition spells out anybody
        else's just as cheaply, so a tenant credential could not be let in
        without letting it write into every other tenant's prefix. Deriving the
        segment is what makes the route safe to open, and opening the route is
        what makes the derivation reachable - neither half stands alone.

        It also removes the obstacle that used to stop the two from being tied
        together: the owner tenant segment and ``sandbox_tenants.id`` were two
        namespaces with nowhere in the code to meet. Derived, they are the same
        string by construction, and the subject segment is 32 lowercase hex,
        which every identifier rule on either side of the boundary accepts.

        Four outcomes, and the two refusals are refusals rather than quiet
        corrections for the same reason ``X-Sandbox-Tenant`` is:

        * management plane (no tenant of its own) - the owner is taken from the
          request, because naming one is the only way that identity can act for
          a tenant at all;
        * tenant-bound credential that named an owner - **403**, whatever it
          named, its own partition included. Silently overwriting the value
          would answer 2xx while filing the bytes somewhere other than where
          the caller recorded them, and the caller would learn otherwise only
          on the request where the two differ;
        * tenant-bound credential with ``X-Acting-Subject`` - the owner is
          ``<tenant from the credential>/<subject from the header>``;
        * tenant-bound credential without it - there is no subject to build a
          partition from. ``required=True`` refuses; ``required=False`` leaves
          the owner unset, for the one route where an owner is optional and its
          absence already means "this token may not touch object storage".
        """
        if self.tenant_id is None:
            self.object_owner = (
                None if supplied is None else control_plane.validate_object_owner(supplied)
            )
            return True
        if supplied is not None:
            self.audit("object.owner", target=str(supplied)[:128], outcome="denied")
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": (
                        "this credential is bound to a tenant; owner is "
                        "derived from the credential and X-Acting-Subject "
                        "and is not accepted in a request"
                    )
                },
            )
            return False
        if self.acting_subject is None:
            self.object_owner = None
            if not required:
                return True
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        "object storage requires X-Acting-Subject: the owner "
                        "is <tenant>/<subject>, the tenant comes from this "
                        "credential and the subject from that header"
                    )
                },
            )
            return False
        self.object_owner = control_plane.validate_object_owner(
            f"{self.tenant_id}/{self.acting_subject}"
        )
        return True

    def forget_workspace_ownership(self, workspace_id: str) -> None:
        """Delete the ownership record and return the quota to the tenant.

        Responsibilities: Only responsible for clearing the attributed rows in the database; not responsible for deleting data on the volume (the caller has already deleted it).

        Constraints: **You cannot report the entire DELETE as failed just because the cleanup failed**. The data has been deleted at this time
             , returning 5xx will make the caller think that nothing happened, so it will hold an empty
             Continue to use Workspace. Errors here are only recorded in the log, and running DELETE again will make up for it.

        When the management identity (tenant_id is None) deletes someone else's Workspace, the owner is looked up from the store -
        You cannot use self.tenant_id as the owner. If it is None, no rows will be cleared.

        Implemented at the module level, forget_workspace_row: reaper's idle recycling does the same thing,
        And it has no handler instance. Both call points share a common implementation."""
        control_plane.forget_workspace_row(workspace_id, owner=self.tenant_id)

    def _workspace_owner_matches(self, workspace_id: str) -> bool | None:
        """Whether the Workspace belongs to the requesting tenant. None = undeterminable (the store is unavailable); the response has been sent.

        Split into its own method because the Runtime ownership check reuses the same query with different wording.
        See require_sandbox_tenant."""
        try:
            owner = control_plane.STORE.owner_of(workspace_id)
            # The break-glass management identity covers both its own reserved
            # tenant rows and the legacy pre-store "no tenant" records.
            return owner == self.tenant_id or (
                self.tenant_id is None and owner == "management"
            )
        except StoreError as exc:
            self.send_store_outage(exc)
            return None

    def require_workspace_tenant(self, workspace_id: str) -> bool:
        """Before operating a Workspace by ID, confirm that it belongs to this tenant.

        List filtering only blocks "seeing", but this block blocks "guessing the ID and then acting directly".
        workspace_id is HMAC-derived and non-enumerable, but non-enumerability is not an access control."""
        if control_plane.STORE is None or self.tenant_id is None:
            return True
        matches = self._workspace_owner_matches(workspace_id)
        if matches:
            return True
        if matches is None:
            return False
        #Deliberately return 404 instead of 403: 403 will tell the caller "this ID exists, it just doesn't belong to you",
        #That's a signal that can be used to enumerate.
        #But the rejection itself leaves a mark - continuous rejection means someone is testing the ID, which is a precursor to an attack.
        #Instead of noise, there is precisely nothing to say in the response.
        self.audit("workspace.access", target=workspace_id, outcome="denied")
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "workspace not found"})
        return False

    def require_sandbox_tenant(self, sandbox_id: str) -> bool:
        """Before operating a runtime by ID, confirm that the Workspace it serves belongs to this tenant.

        The same discipline as require_workspace_tenant, but with one more criterion: Runtime
        If it has no row in the store, ownership can only be read from the workspace-id on the Pod label. So when K8s is unreachable
        At this time, this authentication cannot be performed - KubeError bubbles up to 5xx and is not allowed.

        🔴 Constraints: Whether the label is missing or not, it will be treated as "not found". Do not release it for compatibility with existing Pods.
             Runtimes whose ownership cannot be determined should only be dealt with by the management side and cannot be touched by tenants."""
        if control_plane.STORE is None or self.tenant_id is None:
            return True
        try:
            #🔴 Use get_runtime instead of runtime_owner: the latter only recognizes live records (that’s for
            #delete_runtime back-checking semantics). The determination of ownership depends on "who it once belonged to"——
            # the caller of an asynchronous provisioning polls by ID, and after provisioning fails what it looks up is a
            # terminal-state record. If ownership cannot be recognized the only answer is 404, and 404 cannot distinguish
            # "never provisioned" from "provisioned but failed". Finding the terminal state is not an overreach: the Pod is
            # long gone, and all that can be seen is the state of the caller's own sandbox.
            record = control_plane.STORE.get_runtime(sandbox_id)
        except StoreError as exc:
            self.send_store_outage(exc)
            return False
        owner = record["tenant"] if record else None
        if owner is None:
            # The store has no live record. It may be a Pod that existed before this change shipped; fall back to the Pod label
            #Check back once - the label is also only written by Control Plane, and the criterion is credible.
            #⚠️ This fallback is for the transition period: the TTL of Runtime is only 30 minutes, and the stock Pod
            #It will disappear soon, and you can delete this paragraph later.
            runtime = control_plane.runtime_exists(sandbox_id)
            workspace_id = ""
            if runtime is not None:
                workspace_id = runtime.workspace_id
            if workspace_id:
                matches = self._workspace_owner_matches(workspace_id)
                if matches:
                    return True
                if matches is None:
                    return False
        elif owner == self.tenant_id or (
            self.tenant_id is None and owner == "management"
        ):
            return True
        #The same anti-enumeration rule as the Workspace side: If it does not exist and does not return, you will get the same 404.
        self.audit("sandbox.access", target=sandbox_id, outcome="denied")
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "sandbox not found"})
        return False

    def tolerate_revocation_outage(self, exc: Exception, target: str) -> None:
        """Cancel the accounting when the gate cannot be checked. See scoped_tenant_is_active for an argument for direction.

        It cannot be released silently - then no one will know that the deactivation gate is failing. Reuse
        STORE_ERRORS instead of opening a new indicator: the existing alarm is focused on it, changing the name is equivalent to this one
        The new invalid path will not be viewed by default."""
        control_plane.STORE_ERRORS.inc()
        print(
            f"warning: revocation check skipped for {target}: {exc}",
            flush=True,
        )

    def scoped_tenant_is_active(self, kind: str, subject: str) -> bool:
        """If the tenant is deactivated after issuance, this scoped token must become invalid on the spot.

        Why can't we just rely on TTL: deactivation is a stop loss action, while ACCESS_TOKEN_TTL_SECONDS defaults
        900s - the token caught by the other party one second before deactivation can still be written in the "already deactivated" 900s
        file, run MCP, and export to object storage. _assume_tenant That gate is only in **Control plane credentials**
        Above, the scoped path doesn't work at all.

        The criteria must be stored in the database and cannot be written into the token: deactivation occurs only after issuance, and any claim will
        Fixed at the moment of issuance. The price is one more check for each scoped request (one JOIN, one connection,
        See Store.workspace_tenant_active). **No caching**: The cached TTL is "Revoke
        The window that does not reappear immediately is exactly what needs to be eliminated this time; it is really too expensive and should be changed.
        Store._cursor creates a new connection every time, not this gate.

        🔴 AI-LOCK: **fail open** when the store is unavailable, the opposite direction from the
             fail-closed gates on the control plane. This is intentional. Don't just change it to 503:
             * The gates are blocked by different things. Those block "acting for somebody else", and letting one
               through means a cross-tenant write - a boundary that has never been crossed; what this one blocks is
               "continue to represent oneself after deactivation", and letting it through only
               returns to the state before the fix, opening no unauthorized area.
             * The exposure window has a hard upper limit and does not increase with the downtime: deactivated tenants cannot be signed when the database is unavailable
               Issue new token (require_control_plane_auth / _assume_tenant are all fail closed),
               Only the batch checked out before deactivation can be used, ≤ ACCESS_TOKEN_TTL_SECONDS will expire naturally.
             * On the other hand, fail closed would turn a store hiccup into a full stop of the data plane: this is the
               first store dependency the Control Plane puts on every scoped request. Before this fix the Agent kept reading and
               writing as usual while the store was down. Trading a full tenant lockout for a window already capped at 900s is not worth it.

        ⚠️ This gate is in charge of **tenant deactivation**, but cannot control **key revocation**: the token's claims only
           aud/kind/sub/exp/own, there is no key id to issue it (see issue_access_token), check back
           there is no way to know which key signed it. After a key is revoked, the scoped tokens it issued stay usable until
           TTL expires naturally - this half of the status quo can't be done, don't think it's covered here."""
        if control_plane.STORE is None:
            #The single-tenant model has no concept of tenants, and the behavior is literally the same as before this gate was introduced.
            return True
        try:
            if kind == "sandbox":
                active = control_plane.STORE.runtime_tenant_active(subject)
            elif kind == "workspace":
                active = control_plane.STORE.workspace_tenant_active(subject)
            else:
                #🔴 Add a new scoped kind instead of adding a counter-check here, it will naturally bypass this
                #Gate (take this branch silently = release, no error is reported online).
                # Keep revoked-key rejection on the shared authentication path.
                #All call points of require_scoped_auth will turn red if they do not match this point.
                active = None
        except StoreError as exc:
            self.tolerate_revocation_outage(exc, subject)
            return True
        if active is False:
            self.audit("scope.suspended", target=subject, outcome="denied")
            #403 instead of 401: There is nothing wrong with the token itself, and it is useless to change it. What should be done is to restore the tenant.
            #The tenant name is not returned - the scoped token may have been handed over to the browser, and the
            #subject is enough to locate the problem.
            self.send_json(
                HTTPStatus.FORBIDDEN, {"error": "tenant is suspended"}
            )
            return False
        return True

    def require_scoped_auth(self, kind: str, subject: str) -> bool:
        claims = control_plane.verify_access_token(self.bearer_token(), kind, subject)
        if claims is None:
            self.send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "invalid or expired scoped access token"},
            )
            return False
        self.scoped_credential = True
        if not self.scoped_tenant_is_active(kind, subject):
            return False
        #Provides object routing with owner binding; one handler instance for each request, not shared.
        self.scoped_claims = claims
        return True

    def require_workspace_read_auth(self, workspace_id: str) -> bool:
        """Workspace read-only access: Workspace scoped token or Control Plane token are allowed.

        Responsibility: Only serve the **read** subset of files (read/read-binary/list/glob/grep);
             Write subset (write/write-binary/edit) is the same as objects/import|export
             If you don’t go here, you will still only recognize scoped tokens.

        Why is the Control Plane token allowed to read other people's Workspace? This is deliberately relaxed, take note of it.
        Argument to avoid being "fixed" as a loophole in the future, or to avoid being used as a precedent to continue to expand:
          * Callers holding Control Plane tokens can **DELETE /v1/workspaces/{id}`
            Destroy the entire Workspace along with the files. "Can destroy but not view" the same batch of data
            It cannot stop malicious parties, it can only stop legitimate uses such as operation and maintenance panels.
          * In turn, signing it a Workspace scoped token without owner is even worse:
            See the docstring for issue_access_token - that token is on object storage
            It is equivalent to the administrator, and it has to be handed over to the browser.
          * Do not let go of writing: There is no reason to change the Agent’s work site from the operation and maintenance perspective, but once the writing is broken, the site
            The evidence was lost at the same time.

        Constraint: Relaxation is limited to this method. Any "easy way" to get Control Plane token via require_scoped_auth
             Any changes will also open the cross-owner interface of objects/import|export."""
        #The scoped token comes with workspace binding, and there is only one thing left after verification: does the tenant have it after it is issued?
        #is deactivated. Without it, disabling only blocks the control plane credentials.
        if control_plane.verify_access_token(self.bearer_token(), "workspace", workspace_id):
            self.scoped_credential = True
            return self.scoped_tenant_is_active("workspace", workspace_id)
        #Control plane credentials (static token / admin key / tenant key) follow this method: first identify the person, then confirm
        #This Workspace belongs to it. Without the second half of the sentence, list filtering is in vain - get an ID
        #You can directly read other people's files.
        if not self.require_control_plane_auth():
            return False
        return self.require_workspace_tenant(workspace_id)

    def ticket_owner_is_active(self, claims: dict) -> bool:
        """Deactivation gate for object tickets. It has the same purpose as scoped_tenant_is_active, but the reverse search path is different.

        There is no tenant in the ticket, only owner (key in the form users/<owner tenant segment>/<subject>/...).
        Two kinds of owner reach here and the lookup has to cover both (Store.owner_tenant_active does):
          * derived - the tenant segment **is** a sandbox_tenants.id, because resolve_object_owner built it
            from the credential. This is the whole of the traffic a tenant credential produces.
          * declared - the management plane named the owner itself, so the segment is whatever that identity
            passed and belongs to no tenant by construction. sandbox_owner_prefixes is the only thing that
            matches those two namespaces up.

        ⚠️ Boundary: a declared owner nobody registered cannot be attributed and is let through, so for that
             half this gate is only effective on a deployment that registered its owner prefixes. Derived owners
             need no registration and are always covered.

        What this gate blocks is the batch signed for an owner before its tenant was deactivated and already
        handed out; signing a new one is blocked several steps earlier, when the credential authenticates."""
        owner = control_plane.object_key_owner(claims.get("key"))
        if control_plane.STORE is None or owner is None:
            return True
        try:
            active = control_plane.STORE.owner_tenant_active(owner.split("/", 1)[0])
        except StoreError as exc:
            #The direction is the same as scoped_tenant_is_active, see the argument there.
            self.tolerate_revocation_outage(exc, owner)
            return True
        if active is False:
            self.audit(
                "object.ticket.suspended", target=owner, outcome="denied"
            )
            self.send_json(
                HTTPStatus.FORBIDDEN, {"error": "tenant is suspended"}
            )
            return False
        return True

    def require_object_ticket(self, operation: str) -> dict | None:
        claims = control_plane.verify_object_ticket(self.bearer_token(), operation)
        if claims is None:
            self.send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "invalid, expired, or already used object ticket"},
            )
            return None
        #The deactivation judgment is placed **before consume**: consume will burn this one-time ticket, burn it first and then reject it.
        #rather than let a rejected request destroy the ticket as well - that ticket could never be used again after the tenant was reinstated.
        if not self.ticket_owner_is_active(claims):
            return None
        if not control_plane.consume_object_ticket(claims):
            self.send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "invalid, expired, or already used object ticket"},
            )
            return None
        return claims

    def receive_object_content(self, claims: dict) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        max_bytes = int(claims["max_bytes"])
        if length < 0 or length > max_bytes:
            self.close_connection = True
            raise ValueError("object exceeds ticket size limit")
        digest = hashlib.sha256()
        with tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024,
            mode="w+b",
            dir="/tmp",
        ) as upload:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("request body ended before Content-Length")
                upload.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            actual_digest = digest.hexdigest()
            expected_digest = str(claims.get("sha256") or "")
            if expected_digest and not hmac.compare_digest(
                expected_digest, actual_digest
            ):
                raise ValueError("sha256 does not match content")
            upload.seek(0)
            control_plane.object_put(
                str(claims["bucket"]),
                str(claims["key"]),
                upload,
                content_type=str(claims["content_type"]),
                metadata={"Sha256": actual_digest},
            )
        self.send_json(
            HTTPStatus.CREATED,
            {
                "bucket": claims["bucket"],
                "key": claims["key"],
                "bytes": length,
                "sha256": actual_digest,
                "content_type": claims["content_type"],
            },
        )

    def send_object_content(self, claims: dict) -> None:
        bucket = str(claims["bucket"])
        key = str(claims["key"])
        item = control_plane.object_stat(bucket, key)
        size = int(item.get("size") or 0)
        if size > int(claims["max_bytes"]):
            raise ValueError("object exceeds ticket size limit")
        metadata = item.get("metadata") or {}
        expected_digest = str(metadata.get("X-Amz-Meta-Sha256") or "")
        with control_plane.object_stream(bucket, key) as body:
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    metadata.get(
                        "Content-Type", "application/octet-stream"
                    ),
                )
                self.send_header("Content-Length", str(size))
                if item.get("etag"):
                    self.send_header("ETag", str(item["etag"]))
                if item.get("versionID"):
                    self.send_header(
                        "X-Object-Version-Id", str(item["versionID"])
                    )
                if expected_digest:
                    self.send_header("X-Content-SHA256", expected_digest)
                self.end_headers()

                remaining = size
                digest = hashlib.sha256()
                while remaining:
                    chunk = body.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    digest.update(chunk)
                    remaining -= len(chunk)
                # A body longer than the HEAD said means the object changed
                # under the ticket; short means it was truncated. Both are the
                # same failure as before: the promised Content-Length was not
                # what got written.
                extra = body.read(1)
                if (
                    remaining
                    or extra
                    or (
                        expected_digest
                        and not hmac.compare_digest(
                            expected_digest, digest.hexdigest()
                        )
                    )
                ):
                    self.close_connection = True
                    print(
                        "object download stream failed integrity/size check",
                        flush=True,
                    )
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    @staticmethod
    def match_path(pattern: str, path: str) -> re.Match[str] | None:
        return re.fullmatch(pattern, path)

    def proxy_workspace(
        self,
        method: str,
        workspace_id: str,
        operation: str,
        payload: dict | None = None,
        query: dict[str, str] | None = None,
    ) -> None:
        """Forward the file request to the party that can currently service it.

        Runtime MCP owns the mutable POSIX workspace. There is deliberately
        no volume-agent fallback: a successful response must always describe
        the same filesystem and semantics that shell execution sees."""
        sandbox_id = control_plane.runtime_serving_workspace(workspace_id)
        if sandbox_id:
            status, body, content_type = control_plane.internal_http(
                method,
                f"{control_plane.runtime_endpoint(sandbox_id)}/v1/files/{operation}",
                control_plane.capability_ticket_for("workspace", workspace_id),
                payload,
                query,
            )
            self.send_bytes(status, body, content_type)
            return

        self.send_json(
            HTTPStatus.CONFLICT,
            {
                "error": f"{operation} requires a running Runtime for {workspace_id}",
                "hint": "create a sandbox for this workspace first",
            },
        )

    def proxy_runtime_mcp(
        self,
        sandbox_id: str,
        request_payload: dict,
    ) -> None:
        body = json.dumps(request_payload).encode("utf-8")
        # 🔴 Built from an allow list of the caller's headers, with this
        # platform's cookies removed: the workload on the other side runs a
        # tenant's own code, and a Console session cookie arriving there is a
        # session handed over, no exploit required.
        headers = {
            "Accept": "application/json, text/event-stream",
            **control_plane.forwardable_headers(
                self.headers,
                ("Accept", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"),
            ),
            # 🔴 After forwardable_headers, not inside its allow list: the trace
            # this platform is propagating is its own, and must not be
            # overridable by whatever the caller happened to send.
            **tracing.outbound_headers(),
            "Authorization": (
                f"Bearer {control_plane.capability_ticket_for('runtime', sandbox_id)}"
            ),
            "Content-Type": "application/json",
        }
        request = Request(
            f"{control_plane.runtime_endpoint(sandbox_id)}/mcp",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            response = control_plane.urlopen(request, timeout=45)
        except HTTPError as exc:
            self.send_bytes(
                exc.code,
                exc.read(),
                exc.headers.get("Content-Type", "application/json"),
            )
            return
        except (OSError, TimeoutError, URLError) as exc:
            self.send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"internal service unavailable: {exc}"},
            )
            return

        with response:
            content_type = response.headers.get(
                "Content-Type", "application/json"
            )
            if "text/event-stream" not in content_type:
                self.send_bytes(response.status, response.read(), content_type)
                return

            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                upstream_fd = response.fileno()
                while True:
                    readable, _, _ = select.select(
                        [self.connection, upstream_fd], [], [], 1
                    )
                    if control_plane._SHUTTING_DOWN.is_set():
                        #The SSE flow has no natural end point, and it will hang until the upstream stream is released. Turn off the arrangement and wait.
                        #Requesting closure on the way. If you don't take the initiative to close it, it will consume the entire budget, and
                        #The Endpoint of this Pod has been removed, and this conversation should not have continued in the first place.
                        #Follow it. Closing is only a few seconds earlier - it will still be terminated when the process exits.
                        #The difference is that the disconnection here is clean (the response ends normally, the connection is not reset).
                        self.close_connection = True
                        return
                    if (
                        self.connection in readable
                        and control_plane.connection_closed(self.connection)
                    ):
                        self.close_connection = True
                        return
                    if upstream_fd not in readable:
                        continue
                    chunk = response.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/livez":
            self.send_json(HTTPStatus.OK, {"status": "alive"})
            return
        if path == "/readyz":
            if control_plane._SHUTTING_DOWN.is_set():
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "shutting down"},
                )
                return
            self.send_json(HTTPStatus.OK, {"status": "ready"})
            return
        if path == "/metrics":
            #Same level as /healthz: No authentication required, NetworkPolicy determines who can be caught.
            #The indicator does not contain any tenant identification, see the description at METRICS.
            self.send_bytes(
                HTTPStatus.OK, control_plane.METRICS.render(), metrics_lib.CONTENT_TYPE
            )
            return
        if path == "/healthz":
            #Depends on the health check, not the readiness probe - readiness is /readyz. Here we explore the downstream areas one by one,
            #Either failure is non-200, and the criteria are intentionally stricter than /readyz: its consumers are people and deployments
            #Deployment verification and sandbox_client connectivity probes
            #Connectivity self-test, the price of red reporting is "a certain step failed", not "the only copy was removed"
            #"Loss of flow". The same check is hung on different gates, and the explosion radius differs by an order of magnitude.
            #🔴 Don’t point kubelet’s readinessProbe back here – that’s what was fixed this time
            #That article: The object storage was shaken, and even the exec/list/release of the sandbox that was already running was broken.
            if control_plane._SHUTTING_DOWN.is_set():
                #Even if you close this item in the layout, you still have to report 503: The physical examination should also truthfully say "This copy is being withdrawn."
                #livez does not follow the change: it is a survival probe, and failure will cause a kubelet restart.
                #And we're about to exit normally.
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "shutting down"},
                )
                return
            try:
                database = "unconfigured"
                if control_plane.STORE is not None:
                    control_plane.STORE.check_ready()
                    database = "ok"
                control_plane.configured_runtime_driver().list_runtimes()
                #Probe Path Blank = The endpoint is an implementation such as Hosted S3 that does not have anonymously accessible health endpoints.
                #In that case, report unchecked truthfully and cannot follow up with ok - "It was passed without checking"
                #It is the failure state that needs to be eliminated in this round of storage decoupling.
                object_storage = "unchecked"
                if control_plane.OBJECT_STORE_HEALTH_PATH:
                    with control_plane.urlopen(
                        f"{control_plane.OBJECT_STORE_ENDPOINT}{control_plane.OBJECT_STORE_HEALTH_PATH}",
                        timeout=2,
                    ) as response:
                        if response.status != HTTPStatus.OK:
                            raise RuntimeError("object storage is not ready")
                    object_storage = "ok"
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "database": database,
                        "kubernetes": "ok",
                        "object_storage": object_storage,
                    },
                )
            except StoreError as exc:
                print(f"healthz: control plane store unavailable: {exc}", flush=True)
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "database unavailable", "diagnosis": "database"},
                )
            except KubeError as exc:
                self.send_json(exc.status, {"error": str(exc)})
            except (OSError, RuntimeError, TimeoutError, URLError) as exc:
                # This 503 is the first place to troubleshoot: when the Pod is stuck at 0/1 it is all operators can see.
                # Saying only "object storage unavailable" cannot distinguish three completely different failures -
                # the name cannot be resolved (the cross-cluster bridge is gone/pruned), the name resolves but the
                # connection fails (storage unit down, or a NetworkPolicy blocks it), or it connects but is not ready.
                # That distinction is kept: `diagnosis` classifies it into exactly those three, from the errno phrase.
                #
                # 🔴 What changed is the exit, not the information. /healthz is unauthenticated (ROUTE_AUTH marks it
                # so, protected by NetworkPolicy), and the endpoint address is topology, not diagnosis - anything that
                # can open a TCP connection to this port could read it. The address now goes to stderr, where reading
                # it already requires cluster access, and the response keeps the classification. An operator reading
                # `kubectl logs` gets strictly more than before; a stranger gets the verdict without the map.
                print(
                    "healthz: object storage unavailable at "
                    f"{control_plane.OBJECT_STORE_ENDPOINT}{control_plane.OBJECT_STORE_HEALTH_PATH}: {exc}",
                    flush=True,
                )
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": (
                            "object storage unavailable: "
                            + control_plane.redact_endpoints(str(exc))
                        ),
                        "diagnosis": control_plane.object_store_failure_hint(exc),
                    },
                )
            return
        if path.startswith(grafana_proxy.ROUTE_PREFIX):
            self.handle_grafana_panel(path, parsed.query)
            return
        try:
            if path == "/v1/auth/methods":
                # Public on purpose: a browser with no credential has to learn
                # which sign-in methods exist before it can use one. It exposes
                # switch states only, never the provider's client secret or the
                # audience this Control Plane pins.
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "local_login": control_plane.LOCAL_LOGIN_ENABLED,
                        "oidc": control_plane.OIDC_CONFIG is not None,
                    },
                )
                return
            if path == "/v1/auth/oidc/login":
                self.begin_oidc_login()
                return
            if path == "/v1/auth/oidc/callback":
                self.complete_oidc_login(query_params(parsed))
                return
            if path == "/v1/whoami":
                if not self.require_control_plane_auth():
                    return
                self.send_json(HTTPStatus.OK, self.whoami_view())
                return
            if path == "/v1/templates":
                #Only the ID is returned, not the image reference: what the caller needs is "which ones can be selected".
                #The image reference belongs to the cluster's internal topology, and there is no reason to hand it over.
                if not self.require_control_plane_auth():
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {"templates": sorted(control_plane.available_templates(self.tenant_id))},
                )
                return
            if path == "/v1/storage/content":
                claims = self.require_object_ticket("download")
                if claims is not None:
                    self.send_object_content(claims)
                return
            if path in (
                "/v1/storage/objects",
                "/v1/storage/objects/list",
                "/v1/storage/objects/stat",
                "/v1/storage/objects/versions",
            ):
                if not self.require_control_plane_auth():
                    return
                query = {
                    key: values[0]
                    for key, values in parse_qs(
                        parsed.query, keep_blank_values=True
                    ).items()
                }
                # The owner is overwritten with the one resolve_object_owner
                # decided, never read from the query. Reading it is what would
                # let one tenant's key enumerate another tenant's prefix.
                if not self.resolve_object_owner(query.get("owner")):
                    return
                query["owner"] = self.object_owner
                if path.endswith("/list"):
                    result = control_plane.list_objects(query)
                elif path.endswith("/stat"):
                    result = control_plane.stat_object(query)
                elif path.endswith("/versions"):
                    result = control_plane.list_object_versions(query)
                else:
                    result = control_plane.get_object(query)
                self.send_json(HTTPStatus.OK, result)
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/checkpoints",
                path,
            )
            if match:
                if not self.require_control_plane_auth():
                    return
                workspace_id = match.group(1)
                if not self.require_workspace_tenant(workspace_id):
                    return
                control_plane.touch_workspace(workspace_id)
                self.send_json(
                    HTTPStatus.OK,
                    control_plane.list_workspace_checkpoints(workspace_id),
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/files/"
                r"(read|read-binary|list|glob|grep)",
                path,
            )
            if match:
                workspace_id, operation = match.groups()
                #The read subset is also open to Control Plane tokens; see require_workspace_read_auth for a demonstration.
                #The write subset (write|write-binary|edit in do_POST) does not move.
                if not self.require_workspace_read_auth(workspace_id):
                    return
                # After the gate, before the side effect: an unowned id must
                # not refresh anyone's idle clock.
                control_plane.touch_workspace(workspace_id)
                query = parse_qs(parsed.query, keep_blank_values=True)
                self.proxy_workspace(
                    "GET",
                    workspace_id,
                    operation,
                    query=control_plane.forwarded_query(query),
                )
                return
            if path == "/v1/sandboxes":
                if not self.require_control_plane_auth():
                    return
                pods = control_plane.configured_runtime_driver().list_runtimes()
                self.send_json(
                    HTTPStatus.OK,
                    {"sandboxes": self.scope_sandboxes(pods)},
                )
                return
            if path == "/v1/monitoring":
                if not self.require_control_plane_auth():
                    return
                pods = control_plane.configured_runtime_driver().list_runtimes()
                self.send_json(HTTPStatus.OK, self.monitoring_view(pods))
                return
            if path == "/v1/admin/tenants":
                if not self.require_admin():
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "tenants": [
                            {
                                "id": tenant.id,
                                "display_name": tenant.display_name,
                                "status": tenant.status,
                                "max_workspaces": tenant.max_workspaces,
                                "max_runtimes": tenant.max_runtimes,
                                "workspaces_in_use": control_plane.STORE.count_workspaces(
                                    tenant.id
                                ),
                            }
                            for tenant in control_plane.STORE.list_tenants()
                        ]
                    },
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})/keys", path
            )
            if match:
                if not self.require_admin():
                    return
                #Only the prefix and usage are returned, and the plaintext does not exist after the moment of issuance.
                self.send_json(
                    HTTPStatus.OK, {"keys": control_plane.STORE.list_api_keys(match.group(1))}
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})/owner-tenants",
                path,
            )
            if match:
                if not self.require_admin():
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "owner_tenants": control_plane.STORE.list_owner_prefixes(
                            match.group(1)
                        )
                    },
                )
                return
            if path == "/v1/admin/audit":
                if not self.require_admin():
                    return
                #Management plane audit tail (sandboxctl audit tail consumer of solution 6.3).
                #Read operations are not subject to auditing - tailing the screen by itself will flood the tailed object.
                try:
                    limit = int(query_params(parsed).get("limit", ["100"])[0])
                except ValueError:
                    raise ValueError("limit must be an integer")
                self.send_json(
                    HTTPStatus.OK,
                    {"events": control_plane.STORE.list_audit(limit=limit)},
                )
                return
            if path == "/v1/admin/keys":
                if not self.require_admin():
                    return
                #Only list management plane keys (those with empty tenant_id). Tenant key is listed by tenant,
                #Go to /v1/admin/tenants/{id}/keys - if you mix the two into one table,
                #The question "How many administrator credentials are there in the cluster?" can only be answered by visual filtering.
                #And it's the only reason this table exists.
                self.send_json(
                    HTTPStatus.OK, {"keys": control_plane.STORE.list_api_keys(None)}
                )
                return
            if path == "/v1/admin/templates":
                if not self.require_admin():
                    return
                #Complete records returned from the management interface (including images): admin can already see the cluster topology.
                #And "Which image does this template point to" is exactly what it wants to review.
                #allowed marks line by line whether the image is still in the current whitelist: after the whitelist is tightened
                #These lines will stop taking effect immediately (see available_templates), if not marked
                #The template will disappear from the tenant view and be nowhere to be found.
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "templates": [
                            {**row, "allowed": control_plane.image_is_allowed(row["image"])}
                            for row in control_plane.STORE.list_templates()
                        ],
                        # Built-in templates have no row in the store and cannot be deleted. Return them alongside so the panel does not
                        #They are rendered as "manageable but no response after deletion".
                        "builtin": dict(control_plane.SANDBOX_TEMPLATES),
                    },
                )
                return
            if path == "/v1/workspaces":
                #Workspace lives longer than Runtime (default 6 hours idle) and occupies an exclusive volume
                #Table of contents. It's not a Pod anymore, so this list is the only outlet for its existence -
                #kubectl sees nothing on that path now. Read-only, does not expose scoped token.
                if not self.require_control_plane_auth():
                    return
                attached = control_plane.attached_workspace_ids()
                _, listing, _ = control_plane.volume_agent_request("GET", "/v1/workspaces")
                entries = json.loads(listing).get("workspaces", [])
                try:
                    entries, owners, recorded = self.scope_workspaces(entries)
                except StoreError as exc:
                    self.send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": f"control plane store unavailable: {exc}"},
                    )
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "workspaces": [
                            control_plane.workspace_view(
                                entry,
                                entry.get("id") in attached,
                                tenant_id=owners.get(entry.get("id")),
                                recorded_last_used_at=recorded.get(entry.get("id")),
                            )
                            for entry in entries
                        ]
                    },
                )
                return
            match = self.match_path(r"/v1/sandboxes/(sb-[a-f0-9]{12})", path)
            if match:
                if not self.require_control_plane_auth():
                    return
                sandbox_id = match.group(1)
                if not self.require_sandbox_tenant(sandbox_id):
                    return
                # During asynchronous provisioning the Pod does not exist yet, but the record already does - it is written
                # before the 202 is returned, so anyone holding the id can query the status.
                # The only records that cannot be found belong to pre-existing sandboxes (provisioned before this change shipped); those read the Pod directly.
                record = control_plane.read_runtime_state(sandbox_id)
                if record and record["status"] == "pending":
                    self.send_json(
                        HTTPStatus.OK,
                        {
                            "id": sandbox_id,
                            "status": "pending",
                            "workspace_id": record["workspace_id"],
                            "template": record["template"],
                        },
                    )
                    return
                pod = control_plane.runtime_exists(sandbox_id)
                if pod is None:
                    # The store row is in a terminal state and the Pod is gone - provisioning failed or the sandbox was released. Return the status truthfully,
                    # do not return 404: the caller polls by ID, and 404 cannot distinguish "never provisioned" from
                    # "provisioned but failed"; the former should raise an error, the latter should retry.
                    self.send_json(
                        HTTPStatus.OK,
                        {
                            "id": sandbox_id,
                            "status": record["status"] if record else "unknown",
                        },
                    )
                    return
                self.send_json(HTTPStatus.OK, control_plane.sandbox_view(pod))
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except StoreError as exc:
            self.send_store_outage(exc)
        except KubeError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except control_plane.RuntimeDriverError as exc:
            self.send_runtime_driver_error(exc)
        except control_plane.ObjectStoreBusy as exc:
            self.send_object_store_busy(exc)
        except (OSError, RuntimeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(grafana_proxy.ROUTE_PREFIX):
            self.handle_grafana_panel(path, parsed.query)
            return
        try:
            if path == "/v1/auth/logout":
                if not self.require_control_plane_auth():
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                for cookie in session.clear_cookies(
                    secure=control_plane.CONSOLE_COOKIES_SECURE
                ):
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/v1/storage/tickets":
                if not self.require_control_plane_auth():
                    return
                payload = self.read_json()
                # A ticket is object-bound and is spent without any further
                # identity check, so the owner it carries has to be the one
                # this credential is entitled to and not the one it asked for.
                if not self.resolve_object_owner(payload.get("owner")):
                    return
                self.send_json(
                    HTTPStatus.CREATED,
                    control_plane.issue_object_ticket(
                        {**payload, "owner": self.object_owner}
                    ),
                )
                return
            if path == "/v1/storage/objects":
                if not self.require_control_plane_auth():
                    return
                payload = self.read_json()
                if not self.resolve_object_owner(payload.get("owner")):
                    return
                self.send_json(
                    HTTPStatus.CREATED,
                    control_plane.put_object({**payload, "owner": self.object_owner}),
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/checkpoints",
                path,
            )
            if match:
                if not self.require_control_plane_auth():
                    return
                workspace_id = match.group(1)
                if not self.require_workspace_tenant(workspace_id):
                    return
                control_plane.touch_workspace(workspace_id)
                self.send_json(
                    HTTPStatus.CREATED,
                    control_plane.checkpoint_workspace(workspace_id),
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/checkpoints/"
                r"([a-z0-9][a-z0-9-]{0,62})/restore",
                path,
            )
            if match:
                if not self.require_control_plane_auth():
                    return
                workspace_id, checkpoint_id = match.groups()
                if not self.require_workspace_tenant(workspace_id):
                    return
                control_plane.touch_workspace(workspace_id)
                payload = self.read_json()
                self.send_json(
                    HTTPStatus.OK,
                    control_plane.restore_workspace_checkpoint(
                        workspace_id,
                        checkpoint_id,
                        str(payload.get("sha256") or "") or None,
                    ),
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/objects/import",
                path,
            )
            if match:
                workspace_id = match.group(1)
                if not self.require_scoped_auth("workspace", workspace_id):
                    return
                # The volume marker (.sandbox/last_used_at) is file-service's; the
                # reaper's clock is the store column, refreshed here.
                control_plane.touch_workspace(workspace_id)
                payload = control_plane.bind_object_owner(
                    self.read_json(),
                    control_plane.scoped_object_owner(self.scoped_claims),
                )
                if payload.get("scope") != "upload":
                    raise ValueError("only upload objects can be imported")
                destination = control_plane.validate_workspace_transfer_path(
                    payload.get("destination"),
                    "data",
                )
                if not destination.startswith("data/uploads/"):
                    raise ValueError(
                        "upload destination must start with data/uploads/"
                    )
                object_result = control_plane.get_object(payload)
                runtime_id = control_plane.require_runtime_for(workspace_id, "object import")
                status, body, content_type = control_plane.internal_http(
                    "POST",
                    f"{control_plane.runtime_endpoint(runtime_id)}/v1/files/write-binary",
                    control_plane.capability_ticket_for("workspace", workspace_id),
                    {
                        "path": destination,
                        "content_base64": object_result["content_base64"],
                        "sha256": object_result["sha256"],
                    },
                )
                if status != HTTPStatus.OK:
                    self.send_bytes(status, body, content_type)
                    return
                file_result = json.loads(body)
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace_id": workspace_id,
                        "destination": destination,
                        "object": {
                            key: object_result[key]
                            for key in ("bucket", "key", "bytes", "sha256")
                        },
                        "file": file_result,
                    },
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/objects/export",
                path,
            )
            if match:
                workspace_id = match.group(1)
                if not self.require_scoped_auth("workspace", workspace_id):
                    return
                # See objects/import: the store column is the reaper's clock.
                control_plane.touch_workspace(workspace_id)
                payload = control_plane.bind_object_owner(
                    self.read_json(),
                    control_plane.scoped_object_owner(self.scoped_claims),
                )
                if payload.get("scope") != "agent":
                    raise ValueError("workspace exports require scope=agent")
                archive = payload.get("archive") is True
                if archive:
                    workspace_path = str(payload.get("workspace_path") or "")
                    if workspace_path != "artifacts":
                        raise ValueError("workspace archive path must be artifacts")
                else:
                    workspace_path = control_plane.validate_workspace_transfer_path(
                        payload.get("workspace_path"),
                        "artifacts",
                    )
                runtime_id = control_plane.require_runtime_for(workspace_id, "object export")
                status, body, content_type = control_plane.internal_http(
                    "GET",
                    f"{control_plane.runtime_endpoint(runtime_id)}/v1/files/"
                    f"{'archive' if archive else 'read-binary'}",
                    control_plane.capability_ticket_for("workspace", workspace_id),
                    query={"path": workspace_path},
                    timeout=120 if archive else 40,
                )
                if status != HTTPStatus.OK:
                    self.send_bytes(status, body, content_type)
                    return
                if archive:
                    if content_type != "application/gzip":
                        raise RuntimeError("workspace archive returned an unexpected content type")
                    if not body or len(body) > control_plane.MAX_STREAM_OBJECT_BYTES:
                        raise ValueError("workspace archive exceeds object size limit")
                    object_result = control_plane.put_object_bytes(
                        {**payload, "sha256": hashlib.sha256(body).hexdigest()},
                        body,
                        max_bytes=control_plane.MAX_STREAM_OBJECT_BYTES,
                    )
                else:
                    file_result = json.loads(body)
                    object_result = control_plane.put_object(
                        {
                            **payload,
                            "content_base64": file_result["content_base64"],
                            "sha256": file_result["sha256"],
                        }
                    )
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace_id": workspace_id,
                        "workspace_path": workspace_path,
                        "archive": archive,
                        "object": object_result,
                    },
                )
                return
            if path == "/v1/admin/tenants":
                if not self.require_admin():
                    return
                payload = self.read_json()
                tenant_id = payload.get("id")
                if not isinstance(tenant_id, str):
                    raise ValueError("id must be a string")
                display_name = payload.get("display_name") or tenant_id
                if not isinstance(display_name, str) or len(display_name) > 128:
                    raise ValueError("display_name must be a string up to 128 chars")
                try:
                    tenant = control_plane.STORE.create_tenant(
                        tenant_id,
                        display_name,
                        max_workspaces=int(
                            payload.get("max_workspaces", control_plane.MAX_WORKSPACES)
                        ),
                        max_runtimes=int(payload.get("max_runtimes", control_plane.MAX_RUNTIMES)),
                    )
                except StoreError as exc:
                    # A duplicate tenant id lands here. Return 409 instead of 500: the caller must be able to tell "my request was wrong"
                    # apart from "the store has a problem".
                    self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                self.audit("tenant.create", target=tenant.id)
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "id": tenant.id,
                        "display_name": tenant.display_name,
                        "status": tenant.status,
                        "max_workspaces": tenant.max_workspaces,
                        "max_runtimes": tenant.max_runtimes,
                    },
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})/keys", path
            )
            if match:
                if not self.require_admin():
                    return
                tenant_id = match.group(1)
                payload = self.read_json()
                label = payload.get("label") or "unnamed"
                if not isinstance(label, str) or len(label) > 128:
                    raise ValueError("label must be a string up to 128 chars")
                if control_plane.STORE.get_tenant(tenant_id) is None:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"unknown tenant: {tenant_id}"},
                    )
                    return
                permissions, expires_in = self.key_options(payload)
                try:
                    plaintext, record = control_plane.STORE.issue_api_key(
                        tenant_id,
                        label,
                        permissions=permissions,
                        expires_in_seconds=expires_in,
                    )
                except StoreError as exc:
                    raise ValueError(str(exc)) from exc
                self.audit("key.issue", target=record.id)
                #🔴 This is the only time plaintext appears. It is no longer logged and can no longer be checked -
                #If you lose it, just sign it again instead of trying to get it back.
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "id": record.id,
                        "tenant_id": tenant_id,
                        "label": label,
                        "api_key": plaintext,
                        "permissions": sorted(record.permissions),
                        "expires_at": record.expires_at,
                        "note": "api_key is shown once and cannot be retrieved later",
                    },
                )
                return
            if path == "/v1/admin/keys":
                if not self.require_admin():
                    return
                #🔴 What is issued is the **management interface key**: does not belong to any tenant, visible to all tenants,
                #Tenants can be created and templates can be modified. It is equivalent to giving away the cluster administrator rights.
                #
                #Static SANDBOX_CONTROL_PLANE_TOKEN can adjust this, it is intentional: otherwise the first admin key
                # could never be generated (no key in the store -> nobody can call this -> the first key can never be issued),
                #And "migrating from static tokens to revocable and traceable admin keys" is what's left of it
                #Only purpose. After the migration is completed, this path should be taken offline with it.
                payload = self.read_json()
                label = payload.get("label") or "unnamed"
                if not isinstance(label, str) or len(label) > 128:
                    raise ValueError("label must be a string up to 128 chars")
                permissions, expires_in = self.key_options(payload)
                try:
                    plaintext, record = control_plane.STORE.issue_api_key(
                        None,
                        label,
                        permissions=permissions,
                        expires_in_seconds=expires_in,
                    )
                except StoreError as exc:
                    raise ValueError(str(exc)) from exc
                self.audit("key.issue.admin", target=record.id)
                #The only time plaintext appears is in the same discipline as the tenant key.
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "id": record.id,
                        "tenant_id": None,
                        "label": label,
                        "api_key": plaintext,
                        "permissions": sorted(record.permissions),
                        "expires_at": record.expires_at,
                        "note": "api_key is shown once and cannot be retrieved later",
                    },
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})/status", path
            )
            if match:
                if not self.require_admin():
                    return
                tenant_id = match.group(1)
                status = self.read_json().get("status")
                #Block illegal values here instead of waiting for the store to throw StoreError: that exception has been
                # reported at the end of do_POST as a "store unavailable" 503, but "you sent an unrecognized
                # status" is the caller's problem. Reporting 503 would make it wait for the store to recover.
                if status not in {"active", "suspended"}:
                    raise ValueError("status must be active or suspended")
                if control_plane.STORE.get_tenant(tenant_id) is None:
                    #If the existence is not checked, UPDATE will still be "successful" if it cannot match the row - the panel will display
                    #The restoration was successful and that tenant never existed.
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"unknown tenant: {tenant_id}"},
                    )
                    return
                control_plane.STORE.set_tenant_status(tenant_id, status)
                self.audit("tenant.status", target=tenant_id)
                self.send_json(
                    HTTPStatus.OK, {"id": tenant_id, "status": status}
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})/owner-tenants",
                path,
            )
            if match:
                if not self.require_admin():
                    return
                tenant_id = match.group(1)
                owner_tenant = self.read_json().get("owner_tenant")
                if not isinstance(owner_tenant, str) or not owner_tenant:
                    raise ValueError("owner_tenant must be a non-empty string")
                if control_plane.STORE.get_tenant(tenant_id) is None:
                    #The same discipline as /status: if the existence is not checked, give it to a non-existent tenant.
                    #The registration also returned "successful", but the permissions were not matched at all.
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"unknown tenant: {tenant_id}"},
                    )
                    return
                try:
                    control_plane.STORE.allow_owner_tenant(
                        tenant_id,
                        owner_tenant,
                        created_by=(
                            self.api_key.id
                            if self.api_key
                            else self.BREAK_GLASS_PRINCIPAL
                        ),
                    )
                except StoreError as exc:
                    raise ValueError(str(exc)) from exc
                self.audit(
                    "tenant.owner_tenant.allow",
                    target=f"{tenant_id}:{owner_tenant}",
                )
                self.send_json(
                    HTTPStatus.CREATED,
                    {"tenant_id": tenant_id, "owner_tenant": owner_tenant},
                )
                return
            if path == "/v1/admin/templates":
                if not self.require_admin():
                    return
                payload = self.read_json()
                try:
                    tenant_id, template_id, image = control_plane.validate_template_write(
                        payload
                    )
                except control_plane.TemplateWriteDisabled as exc:
                    self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                if (
                    tenant_id != GLOBAL_TENANT
                    and control_plane.STORE.get_tenant(tenant_id) is None
                ):
                    #Stop typing the wrong tenant name when your hands are shaking, otherwise it will leave a message that no one will ever see.
                    #Orphan records. It is consistent with the criteria for the path that issued the key.
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"unknown tenant: {tenant_id}"},
                    )
                    return
                created_by = (
                    self.api_key.id if self.api_key else self.BREAK_GLASS_PRINCIPAL
                )
                try:
                    control_plane.STORE.put_template(
                        tenant_id, template_id, image, created_by=created_by
                    )
                except StoreError as exc:
                    self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                self.audit(
                    "template.put", target=f"{tenant_id}/{template_id}"
                )
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "tenant_id": tenant_id,
                        "template_id": template_id,
                        "image": image,
                        "created_by": created_by,
                    },
                )
                return
            if path == "/v1/workspaces/resolve":
                if not self.require_control_plane_auth():
                    return
                payload = self.read_json()
                session_id = payload.get("session_id")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or len(session_id) > 256
                ):
                    raise ValueError("session_id must be 1-256 characters")
                principal_kind, principal_id = control_plane.parse_principal(payload, self.acting_subject)
                workspace_id = control_plane.workspace_id_for_session(
                    session_id,
                    tenant_id=self.tenant_id,
                    principal_kind=principal_kind,
                    principal_id=principal_id,
                )
                if not self.require_workspace_tenant(workspace_id):
                    return
                status, listing, content_type = control_plane.volume_agent_request(
                    "GET", "/v1/workspaces"
                )
                if (
                    status != HTTPStatus.OK
                    or "application/json" not in content_type
                ):
                    raise RuntimeError("workspace lookup failed")
                entries = json.loads(listing).get("workspaces", [])
                if not any(entry.get("id") == workspace_id for entry in entries):
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "workspace not found"},
                    )
                    return
                sandbox_id = control_plane.runtime_serving_workspace(workspace_id)
                template = None
                if sandbox_id:
                    pod = control_plane.runtime_exists(sandbox_id)
                    if pod is None:
                        sandbox_id = None
                    else:
                        template = control_plane.sandbox_view(pod).get("template")
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "workspace_id": workspace_id,
                        "sandbox_id": sandbox_id,
                        "template": template,
                    },
                )
                return
            if path == "/v1/workspaces":
                if not self.require_control_plane_auth():
                    return
                payload = self.read_json()
                session_id = payload.get("session_id")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or len(session_id) > 256
                ):
                    raise ValueError("session_id must be 1-256 characters")
                # The owner this workspace token is bound to: it fixes the one
                # object prefix the token can move, so it is resolved the same
                # way every object route resolves it.
                #
                # required=False, unlike the object routes: a tenant credential
                # that names no subject gets a token with no owner claim, which
                # is what it already got before this route resolved an owner at
                # all. That token is not a weaker guard but a total one -
                # bind_object_owner refuses an object operation outright when
                # the claim is missing - so refusing here as well would break
                # callers that never touch object storage in order to protect
                # something already closed.
                if not self.resolve_object_owner(
                    payload.get("owner"), required=False
                ):
                    return
                owner = self.object_owner
                principal_kind, principal_id = control_plane.parse_principal(payload, self.acting_subject)
                workspace_id = control_plane.workspace_id_for_session(
                    session_id,
                    tenant_id=self.tenant_id,
                    principal_kind=principal_kind,
                    principal_id=principal_id,
                )
                if not self.precheck_workspace_quota(workspace_id):
                    return
                workspace_result = control_plane.ensure_workspace(workspace_id)
                #The quota and registration are combined into one transaction, and are still ranked after the directory is created:
                #On the other hand, failure to create a directory will leave a record pointing to a non-existent Workspace.
                #And that record will always occupy the quota, and no inspection will take it away (reaper only takes it from
                # reconciles from the volume to the store, never from the store to the volume). Failure here goes the other way - an extra, unrecorded
                #The empty directory can be collected by the reaper according to the free TTL.
                if not self.admit_workspace_ownership(
                    workspace_id,
                    principal_kind=principal_kind,
                    principal_id=principal_id,
                    session_key=session_id,
                ):
                    return
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace_id": workspace_id,
                        "status": "ready",
                        "created": bool(workspace_result.get("created")),
                        "file_url": (
                            f"/v1/workspaces/{workspace_id}/files"
                        ),
                        "access_token": control_plane.issue_access_token(
                            "workspace", workspace_id, owner
                        ),
                        "access_token_expires_in": control_plane.ACCESS_TOKEN_TTL_SECONDS,
                        "owner": owner,
                    },
                )
                return
            if path == "/v1/sandboxes":
                if not self.require_control_plane_auth():
                    return
                if control_plane._SHUTTING_DOWN.is_set():
                    # For a few seconds after traffic is drained the Endpoints are not yet updated and requests still arrive. Provisioning
                    # takes up to 110s; anything admitted now will certainly not finish, leaving "a pending row in the store
                    # + a running Pod in the cluster + a client that never got the id".
                    # A 503 on the spot asks the caller to retry against another replica instead of waiting two minutes for a reset -
                    # and it is the only option that leaves no residue.
                    # Only provisioning is blocked: other requests (read, delete, MCP forwarding) are short and served as usual.
                    # Shutdown can wait for them.
                    raise KubeError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "control_plane is shutting down; retry the request",
                    )
                payload = self.read_json()
                workspace_id = payload.get("workspace_id")
                if (
                    not isinstance(workspace_id, str)
                    or not control_plane.WORKSPACE_ID.fullmatch(workspace_id)
                ):
                    raise ValueError("invalid workspace_id")
                if not self.require_workspace_tenant(workspace_id):
                    return
                template_id = control_plane.resolve_template(
                    payload, control_plane.available_templates(self.tenant_id)
                )
                # Runtime creation never creates or owns the Workspace.
                #The criterion is changed from "Workspace Pod ready" to "Directory on the volume is in" - Workspace is already
                #Not a Pod anymore. Still need to check: Let "build Runtime against non-existent Workspace"
                #Fail on the spot, instead of relying on initContainer to quietly create an empty one.
                probe_status, _, _ = control_plane.volume_agent_request(
                    "GET",
                    f"/v1/workspaces/{workspace_id}/files/list",
                    query={"path": "."},
                )
                if probe_status == HTTPStatus.NOT_FOUND:
                    raise KubeError(
                        HTTPStatus.NOT_FOUND,
                        f"workspace {workspace_id} does not exist",
                    )
                if probe_status != HTTPStatus.OK:
                    raise RuntimeError(
                        f"workspace probe failed with status {probe_status}"
                    )
                sandbox_id = f"sb-{secrets.token_hex(6)}"
                limit = self.tenant_runtime_limit()
                # The access token is tied only to the sandbox_id and does not depend on whether the Pod exists, so the asynchronous
                # path can hand it to the caller on the spot - the caller can stash the token while provisioning runs
                # and skip the second round trip.
                #The default is still synchronous: changing the default value will make all existing callers in a single deployment
                #Collectively we got 202 and a sandbox that wasn't ready yet. The switch must be initiated by the caller himself.
                if payload.get("wait", True):
                    pod = control_plane.ensure_runtime(
                        sandbox_id,
                        workspace_id,
                        template_id,
                        self.tenant_id or "management",
                        limit,
                    )
                    view = control_plane.sandbox_view(pod)
                    actual_id = view["id"]
                    self.send_json(
                        HTTPStatus.CREATED,
                        {
                            **view,
                            "id": actual_id,
                            "mcp_url": f"/v1/sandboxes/{actual_id}/mcp",
                            "access_token": control_plane.issue_access_token(
                                "sandbox", actual_id
                            ),
                            "access_token_expires_in": control_plane.ACCESS_TOKEN_TTL_SECONDS,
                        },
                    )
                    return
                envelope = {
                    "id": sandbox_id,
                    "mcp_url": f"/v1/sandboxes/{sandbox_id}/mcp",
                    "access_token": control_plane.issue_access_token("sandbox", sandbox_id),
                    "access_token_expires_in": control_plane.ACCESS_TOKEN_TTL_SECONDS,
                }
                control_plane.spawn_runtime_creation(
                    sandbox_id,
                    workspace_id,
                    template_id,
                    self.tenant_id or "management",
                    limit,
                )
                self.send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        **envelope,
                        "status": "pending",
                        "workspace_id": workspace_id,
                        "template": template_id,
                    },
                )
                return
            match = self.match_path(
                r"/v1/sandboxes/(sb-[a-f0-9]{12})/token",
                path,
            )
            if match:
                if not self.require_control_plane_auth():
                    return
                sandbox_id = match.group(1)
                #This scoped token can directly execute the shell on the sandbox, and the ownership determination must be in
                #Before issuance - after signing, it is equivalent to giving out the key.
                if not self.require_sandbox_tenant(sandbox_id):
                    return
                if control_plane.runtime_exists(sandbox_id) is None:
                    raise KubeError(
                        HTTPStatus.NOT_FOUND,
                        f"runtime {sandbox_id} not found",
                    )
                control_plane.touch_runtime(sandbox_id)
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "id": sandbox_id,
                        "access_token": control_plane.issue_access_token(
                            "sandbox", sandbox_id
                        ),
                        "access_token_expires_in": control_plane.ACCESS_TOKEN_TTL_SECONDS,
                    },
                )
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/files/(write|write-binary|edit)",
                path,
            )
            if match:
                workspace_id, operation = match.groups()
                if not self.require_scoped_auth("workspace", workspace_id):
                    return
                control_plane.touch_workspace(workspace_id)
                self.proxy_workspace(
                    "POST",
                    workspace_id,
                    operation,
                    payload=self.read_json(),
                )
                return
            match = self.match_path(
                r"/v1/sandboxes/(sb-[a-f0-9]{12})/mcp",
                path,
            )
            if match:
                sandbox_id = match.group(1)
                if not self.require_scoped_auth("sandbox", sandbox_id):
                    return
                request_payload = self.read_json()
                control_plane.touch_runtime(sandbox_id)
                self.proxy_runtime_mcp(sandbox_id, request_payload)
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except StoreError as exc:
            self.send_store_outage(exc)
        except KubeError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except control_plane.RuntimeDriverError as exc:
            self.send_runtime_driver_error(exc)
        except TimeoutError as exc:
            self.send_json(HTTPStatus.GATEWAY_TIMEOUT, {"error": str(exc)})
        except control_plane.ObjectStoreBusy as exc:
            self.send_object_store_busy(exc)
        except (OSError, RuntimeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/v1/storage/content":
                claims = self.require_object_ticket("upload")
                if claims is not None:
                    self.receive_object_content(claims)
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except KubeError as exc:
            #🔴 Same reason as send_store_outage: KubeError is RuntimeError
            #Subcategory, without this one, it will fall into the lower pocket and become 400. And on this road
            #KubeError comes from consume_object_ticket that Lease write——
            #The apiserver is unreachable, and RBAC lacks the create of leases. It all looks like this.
            #The behavior itself is fail-closed (tickets will not be uploaded until they are consumed), but the reward is 400
            #The caller will be asked to change the request and exchange the ticket, but what really should be done is to try again.
            # Bounds: StoreError is not listed here. The only place where do_PUT hits the store is
            #ticket_owner_is_active, which itself ate the StoreError (see there
            #tolerate_revocation_outage), so StoreError cannot reach here——
            #Listing a branch that cannot be reached is equivalent to writing an assertion that will never be evaluated. Which day
            #A direct STORE call has been added to do_PUT, and this tuple needs to be longer.
            self.send_json(exc.status, {"error": str(exc)})
        except control_plane.ObjectStoreBusy as exc:
            self.send_object_store_busy(exc)
        except (OSError, RuntimeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self.require_control_plane_auth():
            return
        try:
            if path == "/v1/storage/objects":
                if not self.require_control_plane_auth():
                    return
                query = {
                    key: values[0]
                    for key, values in parse_qs(
                        parsed.query, keep_blank_values=True
                    ).items()
                }
                if not self.resolve_object_owner(query.get("owner")):
                    return
                query["owner"] = self.object_owner
                self.send_json(HTTPStatus.OK, control_plane.delete_object(query))
                return
            match = self.match_path(
                r"/v1/workspaces/(ws-[a-f0-9]{12})/checkpoints/"
                r"([a-z0-9][a-z0-9-]{0,62})",
                path,
            )
            if match:
                workspace_id, checkpoint_id = match.groups()
                if not self.require_workspace_tenant(workspace_id):
                    return
                control_plane.touch_workspace(workspace_id)
                self.send_json(
                    HTTPStatus.OK,
                    control_plane.delete_workspace_checkpoint(workspace_id, checkpoint_id),
                )
                return
            match = self.match_path(r"/v1/admin/keys/([a-f0-9]{16})", path)
            if match:
                if not self.require_admin():
                    return
                if not control_plane.STORE.revoke_api_key(match.group(1)):
                    #If the return value is not determined, revoking a key that does not exist will still give a green light——
                    # An operator who mistypes the id would believe the key is revoked while the real one is still live.
                    #(The revoke_owner_tenant route has already prevented the same situation.)
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"no such api key: {match.group(1)}"},
                    )
                    return
                self.audit("key.revoke", target=match.group(1))
                # Idempotent: revoking an already revoked key returns 200 again. A caller retrying after a timeout must not see
                # 404 and conclude its own request was wrong - revoke_api_key returns "does this id exist"
                # rather than "did this call change anything", precisely to keep those two apart.
                self.send_json(
                    HTTPStatus.OK, {"id": match.group(1), "revoked": True}
                )
                return
            match = self.match_path(
                r"/v1/admin/templates/([a-z0-9][a-z0-9-]{0,31})", path
            )
            if match:
                if not self.require_admin():
                    return
                if not control_plane.SANDBOX_IMAGE_REGISTRIES:
                    #The whitelist is not configured = Template management is closed as a whole (Contract § 2.4 "All templates are written
                    #409"), including deletion. It will indeed only narrow the authority and release it, which in itself is harmless.
                    #But having the panel see consistent behavior under the same switch is more important than convenience.
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "template management is disabled: "
                            "SANDBOX_IMAGE_REGISTRIES is not configured"
                        },
                    )
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                tenant_id = query.get("tenant_id", [GLOBAL_TENANT])[0] or GLOBAL_TENANT
                #Idempotent: If you delete something that does not exist, 200 will be returned, but to be honest, nothing was deleted this time - the caller
                #You shouldn't see a 404 when you try again and think you deleted the wrong object.
                deleted = control_plane.STORE.delete_template(tenant_id, match.group(1))
                self.audit(
                    "template.delete",
                    target=f"{tenant_id}/{match.group(1)}",
                    outcome="ok" if deleted else "noop",
                )
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "tenant_id": tenant_id,
                        "template_id": match.group(1),
                        "deleted": deleted,
                    },
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})"
                r"/owner-tenants/([A-Za-z0-9][A-Za-z0-9._@-]{0,127})",
                path,
            )
            if match:
                if not self.require_admin():
                    return
                tenant_id, owner_tenant = match.group(1), match.group(2)
                # Check rowcount: if revoking a non-existent registration returned "success", an operator who mistyped the
                # id would believe the permission is revoked while the real one is still live (revoke_api_key made the same
                # mistake once).
                if not control_plane.STORE.revoke_owner_tenant(tenant_id, owner_tenant):
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"no such owner tenant: {owner_tenant}"},
                    )
                    return
                self.audit(
                    "tenant.owner_tenant.revoke",
                    target=f"{tenant_id}:{owner_tenant}",
                )
                self.send_json(
                    HTTPStatus.OK,
                    {"tenant_id": tenant_id, "owner_tenant": owner_tenant,
                     "revoked": True},
                )
                return
            match = self.match_path(
                r"/v1/admin/tenants/([a-z0-9][-a-z0-9]{0,31})", path
            )
            if match:
                if not self.require_admin():
                    return
                #Deactivate rather than delete: The Workspace under the tenant is still on the volume, directly deleting the record will cause
                #Those directories become unowned. The data is left for disposal.
                #
                #The exact scope of "immediate": control plane credentials (_assume_tenant) and issued scoped
                #token / object ticket (scoped_tenant_is_active, ticket_owner_is_active)
                #They are all blocked at the next request. ⚠️ **Does not include key revocation - removing a key will not
                #Invalidate the scoped tokens it has checked out, and those batches will have to wait for TTL
                #(ACCESS_TOKEN_TTL_SECONDS) expires naturally, see issue_access_token for the reason.
                #
                #This is the same as POST /v1/admin/tenants/{id}/status {"status":"suspended"}
                #Equivalently, it is retained because it has been sent. **There is only one way to restore**——Disable
                #Using DELETE and restoring using POST seems asymmetric, but making the restore "DELETE again"
                #Just switch back" is even worse: an idempotent verb shouldn't be a switch.
                if not control_plane.STORE.set_tenant_status(match.group(1), "suspended"):
                    #If the existence is not checked, UPDATE will still be "successful" if it does not match the row: the panel display is disabled.
                    #Success, there is one more tenant.suspend in the audit saying it is deactivated, and that tenant
                    #Doesn't exist at all. Keep the same criteria as POST .../status.
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"unknown tenant: {match.group(1)}"},
                    )
                    return
                self.audit("tenant.suspend", target=match.group(1))
                self.send_json(
                    HTTPStatus.OK, {"id": match.group(1), "status": "suspended"}
                )
                return
            match = self.match_path(r"/v1/sandboxes/(sb-[a-f0-9]{12})", path)
            if match:
                sandbox_id = match.group(1)
                if not self.require_sandbox_tenant(sandbox_id):
                    return
                control_plane.delete_runtime(sandbox_id)
                self.send_json(
                    HTTPStatus.OK,
                    {"id": sandbox_id, "released": True},
                )
                return
            match = self.match_path(r"/v1/workspaces/(ws-[a-f0-9]{12})", path)
            if match:
                workspace_id = match.group(1)
                if not self.require_workspace_tenant(workspace_id):
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                purge = query.get("purge", [""])[0].lower() in {
                    "1", "true", "yes",
                }
                runtimes = (
                    control_plane.configured_runtime_driver()
                    .list_for_workspace(workspace_id)
                )
                if runtimes:
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "workspace still has active Runtime Pods"},
                    )
                    return
                #🔴 The semantics converge with the architecture. Previously Workspace = Pod + data, DELETE delete
                #The Pod retains the data ("release the calculation and reuse it for next reconstruction"), and the data remains on the PVC. Now
                #Workspace **is** data - there is no calculation to release, and "retaining data" is equivalent to
                #This interface does nothing (actual test: it is still in the list after deleting it).
                #
                #This can be changed because the caller has been checked: sandbox_client never adjusts it (it only deletes
                #objects / checkpoints / sandboxes), the only user is the operation and maintenance panel, and
                #The text of the secondary confirmation on the panel originally said "the files inside will disappear together."
                purged_checkpoints = (
                    control_plane.purge_workspace_checkpoints(workspace_id) if purge else 0
                )
                control_plane.remove_workspace_data(workspace_id)
                #🔴 The ownership record must follow the data, otherwise the tenant’s quota will be permanently
                #Occupy: full -> delete all -> rebuild, directly 429, and in /v1/workspaces
                #Not a single one was visible. It cannot be repaired because only this path will clear the record.
                #
                #Place it after deleting the data: If this step fails, you can make up for it by running DELETE again.
                #(require_workspace_tenant is released based on the record, and the record is still there, so it can be entered;
                #remove_workspace_data is idempotent for directories that no longer exist). in turn
                #If you clear the records first, if you fail to delete the data, you will no longer be able to access it.
                self.forget_workspace_ownership(workspace_id)
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "workspace_id": workspace_id,
                        "released": True,
                        #The data is really gone. The field is reserved so as not to destroy the response structure and the value is true.
                        "data_retained": False,
                        "purged": True,
                        "checkpoints_purged": purged_checkpoints,
                    },
                )
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except StoreError as exc:
            self.send_store_outage(exc)
        except KubeError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except control_plane.RuntimeDriverError as exc:
            self.send_runtime_driver_error(exc)
        except control_plane.ObjectStoreBusy as exc:
            self.send_object_store_busy(exc)
        except (OSError, RuntimeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
