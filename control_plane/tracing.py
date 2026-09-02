"""W3C ``traceparent`` propagation.

One string in, one string out, standard library only. The point of using the
W3C header rather than an invented one is that a deployment that already runs a
tracing-aware gateway or mesh in front of this service gets a continuous trace
for free; an invented header ends at this hop and nothing downstream can put it
back together.

🔴 Every parse failure degrades to a freshly generated trace id. Nothing here
may reject a request. A malformed trace header is a diagnostic problem, and
answering it with a failed request turns the diagnostic tooling into an outage
source - the observability layer must never be a precondition for serving.

The current trace id lives in a ContextVar so that outbound calls pick it up
without every call site having to thread it through. Each request is served on
its own thread and each thread has its own context, so this is per-request.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from typing import Any, Callable
from urllib import request
from urllib.parse import unquote


TRACEPARENT_HEADER = "traceparent"
REQUEST_ID_HEADER = "X-Request-Id"

#: ``00-<32 hex>-<16 hex>-<2 hex>``. Only version 00 is accepted: later
#: versions may change the field layout, and guessing at a format that does not
#: exist yet is how a parser starts accepting something it cannot interpret.
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

#: Trace flags used only when **this** service starts the trace.
#:
#: 🔴 Never applied on top of an inbound header. The flags carry the upstream's
#: sampling decision, so overwriting them is unilaterally reversing a decision
#: somebody already made - and reversing it invisibly: the trace stays
#: connected, no test goes red, and the only symptom is an unexplained change
#: in volume at the collector that nobody can attribute to a hop. We propagate;
#: we do not decide. `01` is used only where no decision exists yet, because
#: `00` would have a tracing-aware gateway discard the span we just started.
_GENERATED_FLAGS = "01"

_CURRENT: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "sandbox_trace", default=("", _GENERATED_FLAGS)
)
_CURRENT_SPAN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sandbox_span", default=""
)


def _otlp_endpoint() -> str:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    return f"{endpoint}/v1/traces" if endpoint else ""


_OTLP_ENDPOINT = _otlp_endpoint()
_OTLP_PROTOCOL = os.getenv(
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
).strip()
if _OTLP_ENDPOINT and _OTLP_PROTOCOL != "http/json":
    raise ValueError(
        "Sandbox native trace exporter supports OTLP protocol http/json only"
    )
_OTLP_TIMEOUT = float(os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT", "10"))
_OTLP_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(
    maxsize=int(os.getenv("OTEL_BSP_MAX_QUEUE_SIZE", "2048"))
)
_EXPORTER_STARTED = False
_EXPORTER_LOCK = threading.Lock()
_DROP_OBSERVER: Callable[[str], None] = lambda _reason: None


def set_drop_observer(observer: Callable[[str], None]) -> None:
    """Connect exporter loss to the service metrics without a circular import."""
    global _DROP_OBSERVER
    _DROP_OBSERVER = observer


def _attribute(key: str, value: object) -> dict[str, object]:
    if isinstance(value, bool):
        encoded: dict[str, object] = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _otlp_headers() -> dict[str, str]:
    raw = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""),
    )
    parsed: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError("OTLP headers must use comma-separated key=value")
        parsed[unquote(key.strip())] = unquote(value.strip())
    return parsed


def _export_batch(spans: list[dict[str, Any]]) -> None:
    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [
                _attribute(
                    "service.name",
                    os.getenv("OTEL_SERVICE_NAME", "sandbox-control-plane"),
                ),
                _attribute(
                    "service.version",
                    os.getenv("OTEL_SERVICE_VERSION", "unknown"),
                ),
            ]},
            "scopeSpans": [{
                "scope": {"name": "sandbox", "version": "1"},
                "spans": spans,
            }],
        }]
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    outbound = request.Request(
        _OTLP_ENDPOINT,
        data=body,
        headers={**_otlp_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(outbound, timeout=_OTLP_TIMEOUT) as response:
        response.read(1024)


def _export_loop() -> None:
    while True:
        first = _OTLP_QUEUE.get()
        batch = [first]
        deadline = time.monotonic() + 1.0
        while len(batch) < 256:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(_OTLP_QUEUE.get(timeout=remaining))
            except queue.Empty:
                break
        try:
            _export_batch(batch)
        except Exception as exc:
            # Telemetry must never be on the serving path. One bounded line per
            # failed batch is still observable without retaining or retrying an
            # unbounded backlog during collector outages.
            print(f"otlp_trace_export_failed error={type(exc).__name__}", flush=True)
            _DROP_OBSERVER("export_error")
        finally:
            for _ in batch:
                _OTLP_QUEUE.task_done()


def _ensure_exporter() -> None:
    global _EXPORTER_STARTED
    if not _OTLP_ENDPOINT or _EXPORTER_STARTED:
        return
    with _EXPORTER_LOCK:
        if _EXPORTER_STARTED:
            return
        threading.Thread(
            target=_export_loop,
            name="otlp-trace-exporter",
            daemon=True,
        ).start()
        _EXPORTER_STARTED = True


def flush(timeout: float = 5.0) -> bool:
    """Wait for completed spans during graceful shutdown; never used by requests."""
    deadline = time.monotonic() + max(0.0, timeout)
    while _OTLP_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _OTLP_QUEUE.unfinished_tasks


class Span:
    """Small OTLP/HTTP JSON span with a no-op exporter when unconfigured."""

    def __init__(
        self,
        name: str,
        *,
        kind: int = 1,
        attributes: dict[str, object] | None = None,
        start_ns: int | None = None,
    ) -> None:
        self.name = name
        self.trace_id, self.flags = _CURRENT.get()
        self.parent_span_id = _CURRENT_SPAN.get()
        self.span_id = new_span_id()
        self.kind = kind
        self.attributes = dict(attributes or {})
        self.start_ns = start_ns or time.time_ns()
        self._token = _CURRENT_SPAN.set(self.span_id)
        self._ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def end(self, *, error: BaseException | None = None, end_ns: int | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        _CURRENT_SPAN.reset(self._token)
        if not _OTLP_ENDPOINT or not self.trace_id or self.flags == "00":
            return
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(end_ns or time.time_ns()),
            "attributes": [_attribute(key, value) for key, value in sorted(self.attributes.items())],
            "status": {"code": 2 if error else 1},
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        if error is not None:
            span["status"]["message"] = type(error).__name__
        _ensure_exporter()
        try:
            _OTLP_QUEUE.put_nowait(span)
        except queue.Full:
            print("otlp_trace_queue_full dropped=1", flush=True)
            _DROP_OBSERVER("queue_full")

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.end(error=exc)


def start_span(
    name: str,
    *,
    kind: int = 1,
    attributes: dict[str, object] | None = None,
    start_ns: int | None = None,
) -> Span:
    return Span(name, kind=kind, attributes=attributes, start_ns=start_ns)


def new_trace_id() -> str:
    """A fresh trace id: 32 lowercase hex, never all zeroes."""
    while True:
        candidate = secrets.token_hex(16)
        if candidate != _ZERO_TRACE:
            return candidate


def new_span_id() -> str:
    """A fresh span id: 16 lowercase hex, never all zeroes."""
    while True:
        candidate = secrets.token_hex(8)
        if candidate != _ZERO_SPAN:
            return candidate


def parse_traceparent(value: Any) -> tuple[str, str] | None:
    """``(trace id, flags)`` from a ``traceparent`` header, or None if unusable.

    The flags come back with the trace id because they have to be carried
    onward unchanged; returning only the trace id is what would make dropping
    them the path of least resistance.

    None means "there was no usable header", never "reject this request". The
    caller falls through to the next source.

    All-zero trace and span ids are invalid by the W3C specification, and they
    are the shape a broken producer emits most often - accepting one would make
    every such request share a single trace id, which is worse than having none.
    """
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.fullmatch(value.strip())
    if match is None:
        return None
    trace_id, span_id, flags = match.group(1), match.group(2), match.group(3)
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id, flags


def derive_trace_id(request_id: str) -> str:
    """Turn a request id into a trace id, deterministically.

    The same request id must produce the same trace id in every service that
    sees it, which is what lets a trace be reassembled across services that were
    only given the older header. Hence a fixed rule - sha256 of the UTF-8 bytes,
    first 16 bytes, hex - rather than anything that depends on local state.
    """
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()[:16].hex()
    # Unreachable in practice; a derived all-zero id would be invalid downstream
    # for exactly the reason parse_traceparent rejects one.
    return new_trace_id() if digest == _ZERO_TRACE else digest


def inbound_context(headers: Any) -> tuple[str, str, str]:
    """Return trace id, flags and the upstream parent span id."""
    raw = _header(headers, TRACEPARENT_HEADER)
    parsed = parse_traceparent(raw)
    if parsed is not None:
        match = _TRACEPARENT.fullmatch(raw.strip())
        assert match is not None
        return parsed[0], parsed[1], match.group(2)
    request_id = (_header(headers, REQUEST_ID_HEADER) or "").strip()
    if request_id:
        return derive_trace_id(request_id), _GENERATED_FLAGS, ""
    return new_trace_id(), _GENERATED_FLAGS, ""


def inbound_trace(headers: Any) -> tuple[str, str]:
    """``(trace id, flags)`` for one inbound request, by order of preference.

    1. a well-formed ``traceparent`` - its flags are inherited verbatim
    2. an ``X-Request-Id``, deterministically derived - our own flags
    3. a freshly generated id - our own flags

    Cases 2 and 3 are traces this service starts, so there is no upstream
    decision to preserve. Always returns a usable pair; there is no failure
    mode.
    """
    trace_id, flags, _parent_span_id = inbound_context(headers)
    return trace_id, flags


def inbound_trace_id(headers: Any) -> str:
    """The trace id alone, for callers that do not propagate onward."""
    return inbound_trace(headers)[0]


def _header(headers: Any, name: str) -> str:
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def traceparent(trace_id: str, flags: str = _GENERATED_FLAGS) -> str:
    """The header to send on one outbound call.

    The trace id and the flags are carried through unchanged; only the span id
    is new, which is what makes the hops distinguishable within one trace.
    """
    return f"00-{trace_id}-{new_span_id()}-{flags}"


def set_current(
    trace_id: str,
    flags: str = _GENERATED_FLAGS,
    parent_span_id: str = "",
) -> None:
    _CURRENT.set((trace_id, flags))
    _CURRENT_SPAN.set(parent_span_id)


def current_trace_id() -> str:
    """The trace id of the request being served, or "" outside a request."""
    return _CURRENT.get()[0]


def current_flags() -> str:
    """The trace flags in force, inherited from upstream where there were any."""
    return _CURRENT.get()[1]


def current_span_id() -> str:
    """Active local span id, used when work crosses a thread boundary."""
    return _CURRENT_SPAN.get()


def outbound_headers() -> dict[str, str]:
    """``traceparent`` for the current request, or nothing outside one.

    Returning an empty mapping rather than inventing a trace id keeps a
    background task - the reaper, a startup probe - from emitting a header that
    claims to belong to a trace nobody started.
    """
    trace_id, flags = _CURRENT.get()
    if not trace_id:
        return {}
    span_id = _CURRENT_SPAN.get() or new_span_id()
    return {TRACEPARENT_HEADER: f"00-{trace_id}-{span_id}-{flags}"}
