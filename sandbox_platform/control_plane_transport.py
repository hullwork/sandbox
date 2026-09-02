"""Stateless HTTP transport shared by the SDK and operator tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

try:  # Optional: SDK remains dependency-free when OpenTelemetry is absent.
    from opentelemetry import propagate as _otel_propagate
except Exception:  # pragma: no cover - the normal minimal installation
    _otel_propagate = None


class ControlPlaneError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


#: Headers whose value is decided by the transport's identity, not by whoever
#: happens to be making one call.
#:
#: 🔴 Each of these has exactly one source, and a per-call header may not become
#: a second one. ``Authorization`` comes from ``token``; ``X-Acting-Subject``
#: comes from ``default_headers``. Before this, a per-call ``headers`` mapping
#: was merged last and could silently replace the credential - and a request
#: sent under the wrong identity looks exactly like one sent under the right
#: one, because the response says nothing about which was used.
RESERVED_HEADERS = frozenset({"authorization", "x-acting-subject"})


class ControlPlaneTransport:
    def __init__(
        self,
        base_url: str,
        token: str,
        default_headers: dict[str, str] | None = None,
    ):
        """``default_headers`` travel with the transport's identity.

        Anything that describes *who this request is from* belongs here rather
        than at a call site: the pseudonymous subject it acts for, for instance.
        Threading that through each call is an invitation to miss one, and
        missing one is silent - the call simply arrives as though no subject
        were named, which is what a client acting for nobody also looks like.

        A transport is built per request, and the subject comes from the
        ambient scope (``sandbox_client.acting_subject_context``) rather than
        from a long-lived attribute. One transport therefore still carries
        exactly one identity, which is what the reservation below assumes.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_headers = dict(default_headers or {})
        conflicting = sorted(
            name for name in self.default_headers if name.lower() == "authorization"
        )
        if conflicting:
            raise ValueError(
                "Authorization is set from the token argument; remove it from "
                f"default_headers ({conflicting[0]})"
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 100.0,
    ) -> tuple[dict, str]:
        if query:
            path = f"{path}?{urllib.parse.urlencode(query)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {
            "Accept": "application/json",
            **self.default_headers,
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        for name, value in (headers or {}).items():
            if name.lower() in RESERVED_HEADERS:
                # Raised, not dropped: a caller that meant to change the
                # identity has to find out here, rather than at whatever the
                # server decides to do with the identity it actually got.
                raise ValueError(
                    f"{name} is set by the transport and cannot be overridden "
                    "per call"
                )
            request_headers[name] = value
        if _otel_propagate is not None:
            # Adopt whichever SDK/provider the embedding application configured.
            # The Sandbox SDK never owns exporter setup or sampling policy.
            _otel_propagate.inject(carrier=request_headers)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get(
                    "Content-Type", "application/json"
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("error")
            except (json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise ControlPlaneError(
                exc.code, detail or f"Control Plane returned HTTP {exc.code}"
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise ControlPlaneError(
                502, f"Sandbox Control Plane unavailable at {self.base_url}: {exc}"
            ) from exc

        if "application/json" in content_type:
            try:
                return json.loads(raw) if raw else {}, content_type
            except json.JSONDecodeError as exc:
                raise ControlPlaneError(502, "Control Plane returned invalid JSON") from exc
        return {"raw": raw.decode("utf-8", errors="replace")}, content_type
