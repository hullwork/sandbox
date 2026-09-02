# Observability artifacts

The sandbox platform exposes its own telemetry and ships the artifacts needed
to consume it. It does not ship a monitoring stack.

## What the control plane exposes

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /metrics` | none (NetworkPolicy) | Prometheus text format, `control_plane/metrics.py` |
| `GET /livez` | none | process answers HTTP; kubelet liveness |
| `GET /readyz` | none | should this replica take traffic (drain flag only) |
| `GET /healthz` | none | downstream reachability, for humans and deploy verification. The 503 body carries a classified `diagnosis`, not the address that failed - that goes to the process log, because this endpoint is unauthenticated |

Logs go to stdout/stderr as plain lines. There is no built-in log forwarder;
point your collector at the container's stdout.

Counters are pre-registered at zero for every known label combination
(`control_plane/metrics.py::Counter.ensure`), so "never failed" is a real reading
rather than an absent series - otherwise it would be indistinguishable from
"this build does not emit that metric".

Constraint: **no series carries a tenant, workspace or sandbox identifier.** That is what
makes an unauthenticated `/metrics` acceptable, and it is enforced by
`tests/test_observability_artifacts.py` for the alert rules. Per-tenant numbers
come from the authenticated `/v1/admin` routes. Adding a tenant label means
moving the endpoint behind authentication first - and the label cardinality would
then grow without bound with the tenant count.

## Files

| File | Use |
|---|---|
| `alerts/sandbox-control-plane-rules.yaml` | `kubectl apply` (Prometheus Operator), or lift `.spec.groups` into `rule_files` |
| `scrape/servicemonitor.yaml` | ServiceMonitor plus an equivalent static `scrape_config` |
| `dashboards/sandbox-control-plane.json` | import into Grafana; pick a Prometheus datasource on import |

Alert rules select on `job=~"sandbox-control-plane.*"`. `tests/test_observability_artifacts.py`
checks every `sandbox_*` name in the rules and the dashboard against the
registrations in `control_plane/core.py`, because a rule naming a metric nothing emits
does not fail - it evaluates to an empty vector forever and reports as healthy.

## Tracing

The Control Plane and volume role export sampled spans with standard OTLP/HTTP
JSON when `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is configured. The exporter is a
bounded, non-blocking background batch: collector failure can drop telemetry but
cannot hold a request or grow memory without limit. Drops are counted by
`sandbox_trace_export_drops_total{reason="export_error|queue_full"}`.

The trace covers the inbound Control Plane request, asynchronous provisioning,
the Kubernetes create/readiness phases, runtime health, internal HTTP hops to the
Runtime and volume role, and object-store client operations. W3C sampling flags
are inherited; an upstream `00` is propagated but not exported.

Helm example:

```sh
helm upgrade --install sandbox charts/sandbox \
  --set-string observability.tracing.endpoint=http://otel-collector.observability.svc:4318/v1/traces
```

Kustomize deployments can create an optional `sandbox-observability` ConfigMap
in both `sandbox-system` and `sandbox-workloads`, containing
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json`
and the role-specific `OTEL_SERVICE_NAME`. No ConfigMap means export is disabled;
trace-id log correlation remains available.

## Known gaps

- Object-storage calls go out through `boto3`, which does not yet propagate
  `traceparent`, so that hop is not traced yet. The local operation is a span,
  but the S3/RGW server side is not its child.
- Worker node provisioning is outside the Sandbox product boundary. The cluster's
  node-pool provider must emit the autoscaler decision/VM bootstrap/CNI/CSI-ready
  spans; `sandbox_runtime_create_phase_seconds{phase="pod_ready"}` begins only
  after Sandbox admission.
- Logs are still plain lines rather than OTLP logs. Every request line carries a
  trace id, so Loki/Tempo correlation is possible, but structured JSON logs are
  the next refinement.

## Embedded panels in the console (optional)

The console can render the dashboard above inline, as same-origin iframes. It is
off unless configured, and it is an **operator** feature: the metrics carry no
tenant dimension, so every panel is cross-tenant data and only an administrator
is offered it - enforced server-side on every request, not by hiding a tab.

| Variable | Required | Meaning |
|---|---|---|
| `SANDBOX_GRAFANA_URL` | yes | `http(s)://` origin of your Grafana. No credentials, query or fragment. |
| `SANDBOX_GRAFANA_TOKEN` / `SANDBOX_GRAFANA_TOKEN_FILE` | yes | Service-account token. **Viewer**, scoped to the folder holding this dashboard. |
| `SANDBOX_GRAFANA_DATASOURCE_UID` | yes | The datasource the panels query. |
| `SANDBOX_GRAFANA_DASHBOARD_UID` | no | Defaults to the shipped dashboard's uid. |
| `SANDBOX_GRAFANA_ORG_ID` | no | Defaults to 1. |

All three of the first group or nothing: with a URL and no token the iframe fills
with 401s, which is worse than an absent tab, and without the datasource uid the
proxy cannot bound what `/api/ds/query` may reach (below).

### What the proxy will and will not forward

Requests go to ``/grafana/`` on this origin; the browser never holds a Grafana
credential and never learns a Grafana address. ``control_plane/grafana_proxy.py`` holds a **closed
allowlist** of the paths a solo panel needs - the panel document, Grafana's own
bundle, its frontend settings, the dashboard model, plugin settings, and
`POST /api/ds/query`. Everything else is 403.

Constraint: the allowlist is a code constant on purpose. Proxying `/grafana/*` wholesale
while attaching the service-account token would republish the entire Grafana API
- including `/api/datasources/proxy/...`, which is "run any query against any
datasource" - to every administrator of this console. A configurable allowlist is
the same hole with a delay on it.

Constraint: `POST /api/ds/query` is checked in the **body**, not just by URL. It is
Grafana's generic query endpoint and dispatches on a datasource uid inside the
request, so it is only read-only for the datasource it names: a Grafana with a
SQL datasource attached would otherwise turn that one endpoint into arbitrary
SQL, and a folder-scoped Viewer does not stop it because OSS Grafana does not
scope datasource permissions by folder. The proxy parses the body and refuses any
query naming a uid other than the configured one - and refuses the whole request,
not just the offending query, because forwarding the rest would still have run it.

Credentials do not cross in either direction: the console session cookie is
dropped before the request leaves, and Grafana's `Set-Cookie` is dropped before
the response returns.

### Framing policy

The console's own CSP is untouched - still `default-src 'self'` with
`frame-ancestors 'none'`, and no `frame-src`. The proxied panel response carries
its own narrower policy with `frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN`, because that response *is* the framed document and
the console's `'none'` would make the browser refuse to render our own iframe.

## What this directory is not

It is not a monitoring stack. There is no Prometheus, no Loki, no Grafana and no
Alertmanager here, and no script that installs one. Collection belongs to
whoever operates the service: they may already run kube-prometheus-stack, or
CloudWatch, or Alibaba Cloud SLS, and shipping a second collector would either
be ignored or fight the first one.

The split is deliberate:

| | Owner | Lives in |
|---|---|---|
| Instrumentation (`/metrics`, structured logs, health endpoints) | this repository | the service code |
| Artifacts describing how to consume it (alerts, dashboards, scrape config) | this repository | `observability/` |
| The collector, its storage, its retention, its notification channels | the operator | wherever they like |

`/metrics` is a pull endpoint on purpose. A push exporter would bake a specific
collector's address and protocol into the product.
