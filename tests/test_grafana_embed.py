"""The embedded panel proxy must not become a Grafana API key with a UI.

Almost every case here is negative, and that ratio is the point: this feature
adds a route that attaches a Grafana service-account token to outbound
requests. A proxy that merely renders a panel has already failed if it also
forwards the Console session cookie, answers a tenant, or passes
``/api/datasources/proxy/`` - each of which turns one dashboard into the whole
Grafana API.

Shape follows tests/test_api_authorization.py: the Control Plane runs in a subprocess
against a SQLite control plane with a stub Grafana beside it, so the identity
checks are the real ones rather than a fake handler.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "observability" / "dashboards" / "sandbox-control-plane.json"

PROBE = textwrap.dedent(
    """
    import json
    import os
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from control_plane import kube
    # No cluster in this test. Every call fails loudly rather than silently:
    # the panel proxy never touches Kubernetes, so a call reaching here would
    # mean the request took a route it had no business taking.
    class FakeKube:
        def __init__(self):
            pass

        def _fail(self, *_args, **_kwargs):
            raise kube.KubeError(503, "kubernetes unavailable in this test")

        list = get = patch_annotations = create_or_get = delete = _fail

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import grafana_proxy
    from control_plane import core as control_plane
    control_plane.STORE.ensure_schema()

    SEEN = []

    class GrafanaStub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            SEEN.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            payload = b"<html>panel</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # Grafana really does set a session cookie here. It must not land on
            # the Console's origin.
            self.send_header("Set-Cookie", "grafana_session=abc; Path=/")
            self.send_header("X-Grafana-Internal", "leaked")
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _answer
        do_POST = _answer

    grafana = ThreadingHTTPServer(("127.0.0.1", 0), GrafanaStub)
    threading.Thread(target=grafana.serve_forever, daemon=True).start()
    grafana_url = "http://127.0.0.1:%d" % grafana.server_address[1]

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_port

    def call(method, path, token=None, headers=None, body=None):
        sent = {"Content-Type": "application/json"}
        if token:
            sent["Authorization"] = "Bearer " + token
        # The Console session cookie always rides along: proving it is dropped
        # is the point of the credential-isolation assertions.
        sent["Cookie"] = "sandbox_console_session=console-value"
        sent.update(headers or {})
        request = urllib.request.Request(
            base + path, method=method, data=body, headers=sent
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    admin = os.environ["SANDBOX_CONTROL_PLANE_TOKEN"]
    datasource = "prom-uid"

    def query_body(uid=datasource):
        # A body the datasource check accepts: one query naming the declared
        # datasource.
        return json.dumps({
            "queries": [
                {"refId": "A", "datasource": {"uid": uid, "type": "prometheus"}}
            ]
        }).encode()

    results = {}
    panel = "/grafana/d-solo/sandbox-control-plane/panel"
    try:
        # A tenant credential: authenticated, and not an administrator.
        call("POST", "/v1/admin/tenants", admin,
             body=json.dumps({"id": "acme", "display_name": "Acme"}).encode())
        status, _, payload = call(
            "POST", "/v1/admin/tenants/acme/keys", admin,
            body=json.dumps({"label": "tenant"}).encode())
        tenant_key = json.loads(payload)["api_key"] if status < 300 else None
        results["tenant_key_minted"] = bool(tenant_key)

        # --- unconfigured deployment -----------------------------------
        results["unconfigured_admin"] = call("GET", panel, admin)[0]
        results["unconfigured_whoami"] = json.loads(
            call("GET", "/v1/whoami", admin)[2]
        ).get("grafana")
        os.environ["SANDBOX_GRAFANA_URL"] = grafana_url
        results["half_configured_admin"] = call("GET", panel, admin)[0]
        os.environ["SANDBOX_GRAFANA_TOKEN"] = "grafana-sa-token"
        # Still unconfigured: without a named datasource the proxy cannot bound
        # what /api/ds/query may reach, so it refuses to enable at all.
        results["no_datasource_admin"] = call("GET", panel, admin)[0]
        os.environ["SANDBOX_GRAFANA_DATASOURCE_UID"] = datasource

        # --- access control --------------------------------------------
        results["anonymous"] = call("GET", panel)[0]
        results["tenant"] = call("GET", panel, tenant_key)[0]
        # An admin credential acting as a tenant is not an administrator.
        results["admin_acting_as_tenant"] = call(
            "GET", panel, admin, headers={"X-Sandbox-Tenant": "acme"})[0]
        results["seen_before_admin"] = len(SEEN)

        status, headers, payload = call("GET", panel, admin)
        results["admin"] = status
        results["admin_body"] = payload.decode("utf-8", "replace")
        results["admin_headers"] = {k.lower(): v for k, v in headers.items()}
        results["upstream"] = SEEN[-1] if SEEN else None

        # --- allowlist --------------------------------------------------
        forbidden = [
            ("GET", "/grafana/api/datasources/proxy/1/api/v1/query?query=up"),
            ("GET", "/grafana/api/datasources"),
            ("GET", "/grafana/api/admin/settings"),
            ("GET", "/grafana/api/admin/users"),
            ("GET", "/grafana/api/auth/keys"),
            ("GET", "/grafana/api/orgs"),
            ("GET", "/grafana/api/users"),
            ("GET", "/grafana/d/sandbox-control-plane/panel"),
            ("GET", "/grafana/login"),
            ("GET", "/grafana/"),
            ("POST", "/grafana/api/dashboards/db"),
            ("GET", "/grafana/../api/admin/settings"),
            ("GET", "/grafana//evil.example/"),
        ]
        before = len(SEEN)
        results["forbidden"] = [
            [path, call(method, path, admin,
                        headers={"Sec-Fetch-Site": "same-origin"},
                        body=b"{}" if method == "POST" else None)[0]]
            for method, path in forbidden
        ]
        results["forbidden_reached_grafana"] = len(SEEN) - before

        allowed = [
            ("GET", "/grafana/d-solo/sandbox-control-plane/panel?panelId=10"),
            ("GET", "/grafana/public/build/runtime.js"),
            ("GET", "/grafana/api/frontend/settings"),
            ("GET", "/grafana/api/dashboards/uid/sandbox-control-plane"),
            ("POST", "/grafana/api/ds/query"),
        ]
        results["allowed"] = [
            [path, call(method, path, admin,
                        headers={"Sec-Fetch-Site": "same-origin"},
                        body=query_body() if method == "POST" else None)[0]]
            for method, path in allowed
        ]

        # --- CSRF substitute for the one non-GET entry ------------------
        before = len(SEEN)
        results["cross_site_post"] = call(
            "POST", "/grafana/api/ds/query", admin,
            headers={"Sec-Fetch-Site": "cross-site"}, body=query_body())[0]
        # 🔴 The fail-open this check exists to avoid: "check one, else the
        # other, else allow" admits anything that omits both. Refusing that
        # turns away every page-driven cross-origin request; a caller that
        # composes its own request can send Origin anyway and is a session-theft
        # problem, not a CSRF one.
        results["headerless_post"] = call(
            "POST", "/grafana/api/ds/query", admin, body=query_body())[0]
        results["origin_only_post"] = call(
            "POST", "/grafana/api/ds/query", admin,
            headers={"Origin": base}, body=query_body())[0]
        results["csrf_reached_grafana"] = len(SEEN) - before - 1

        # --- datasource scope of the one generic query endpoint ---------
        def ds_post(body):
            return call("POST", "/grafana/api/ds/query", admin,
                        headers={"Sec-Fetch-Site": "same-origin"}, body=body)[0]

        before = len(SEEN)
        results["ds_declared"] = ds_post(query_body())
        results["ds_other"] = ds_post(query_body("some-mysql-datasource"))
        results["ds_mixed"] = ds_post(json.dumps({"queries": [
            {"refId": "A", "datasource": {"uid": datasource}},
            {"refId": "B", "datasource": {"uid": "some-mysql-datasource"}},
        ]}).encode())
        results["ds_uncertain"] = {
            name: ds_post(body) for name, body in {
                "not json": b"<html>",
                "not an object": b"[]",
                "no queries": b"{}",
                "empty queries": b'{"queries": []}',
                "no datasource": b'{"queries": [{"refId": "A"}]}',
                "legacy numeric id":
                    b'{"queries": [{"refId": "A", "datasourceId": 1}]}',
                "uid is not a string":
                    b'{"queries": [{"datasource": {"uid": 1}}]}',
            }.items()
        }
        # Only the one accepted body should have travelled.
        results["ds_reached_grafana"] = len(SEEN) - before

        # --- W3C trace context on the Grafana hop ------------------------
        TR = "4bf92f3577b34da6a3ce929d0e0e4736"

        def trace_of(headers):
            before = len(SEEN)
            call("GET", panel, admin, headers=headers)
            if len(SEEN) == before:
                return None
            return SEEN[-1]["headers"].get("traceparent")

        results["trace_none"] = trace_of({})
        results["trace_inherit_sampled"] = trace_of(
            {"traceparent": "00-%s-00f067aa0ba902b7-01" % TR})
        results["trace_inherit_unsampled"] = trace_of(
            {"traceparent": "00-%s-00f067aa0ba902b7-00" % TR})
        results["trace_mixed_case"] = trace_of(
            {"TraceParent": "00-%s-00f067aa0ba902b7-01" % TR})
        results["trace_from_request_id"] = trace_of({"X-Request-Id": TR})
        results["trace_malformed"] = trace_of({"traceparent": "00-not-a-trace-01"})
        results["inbound_trace"] = TR

        # --- the Console's own policy is untouched ----------------------
        results["console_headers"] = {
            k.lower(): v for k, v in call("GET", "/livez")[1].items()
        }
        results["whoami_configured"] = json.loads(
            call("GET", "/v1/whoami", admin)[2]
        ).get("grafana")
        results["whoami_tenant"] = json.loads(
            call("GET", "/v1/whoami", tenant_key)[2]
        ).get("grafana")
    finally:
        server.shutdown()
        server.server_close()
        grafana.shutdown()
        grafana.server_close()
    print(json.dumps(results))
    """
)


def run_probe() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-admin-token",
            "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED": "true",
            "SIGNING_KEY": "0" * 32,
            "WORKSPACE_ID_KEY": "1" * 32,
            "SANDBOX_STORE_BACKEND": "sqlite",
            "SANDBOX_STORE_PATH": os.path.join(directory, "control-plane.db"),
            "VOLUME_AGENT_URL": "http://127.0.0.1:1",
            "VOLUME_AGENT_TOKEN": "test-volume-token",
            "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
            "OBJECT_STORE_ACCESS_KEY": "test-access",
            "OBJECT_STORE_SECRET_KEY": "test-secret",
            "PYTHONPATH": str(ROOT),
        }
        for name in ("SANDBOX_CONTROL_PLANE_ROLE", "SANDBOX_GRAFANA_URL", "SANDBOX_GRAFANA_TOKEN",
                     "SANDBOX_GRAFANA_TOKEN_FILE", "SANDBOX_GRAFANA_ORG_ID",
                     "SANDBOX_GRAFANA_DASHBOARD_UID",
                     "SANDBOX_GRAFANA_DATASOURCE_UID"):
            environment.pop(name, None)
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


# Address shape, not the word: `grafana: GrafanaCapability` is a legitimate
# TypeScript annotation in the component this guards, and the first version of
# the guard matched it.
HOST_PORT = re.compile(r"[A-Za-z0-9.-]*grafana[A-Za-z0-9.-]*:\d{2,5}")


class PanelProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def test_the_probe_had_a_tenant_credential_to_test_with(self) -> None:
        """Self-check: without it the "tenant is refused" case would pass vacuously."""
        self.assertTrue(self.results["tenant_key_minted"])

    def test_an_unauthenticated_request_is_refused(self) -> None:
        self.assertEqual(self.results["anonymous"], 401)

    def test_a_tenant_is_refused(self) -> None:
        self.assertEqual(self.results["tenant"], 403)

    def test_an_admin_credential_acting_as_a_tenant_is_refused(self) -> None:
        """Carrying X-Sandbox-Tenant means acting as that tenant, which is not an administrator."""
        self.assertEqual(self.results["admin_acting_as_tenant"], 403)

    def test_nothing_reached_grafana_before_an_administrator_asked(self) -> None:
        self.assertEqual(self.results["seen_before_admin"], 0)

    def test_an_administrator_reaches_the_panel(self) -> None:
        """Mutation anchor for the three refusals above."""
        self.assertEqual(self.results["admin"], 200)
        self.assertIn("panel", self.results["admin_body"])

    def test_every_non_panel_path_is_refused(self) -> None:
        for path, status in self.results["forbidden"]:
            with self.subTest(path=path):
                self.assertEqual(status, 403)
        self.assertEqual(
            self.results["forbidden_reached_grafana"], 0,
            "a non-allowlisted path was forwarded upstream",
        )

    def test_the_paths_a_panel_needs_are_allowed(self) -> None:
        """Mutation anchor: an allowlist that refuses everything also passes the test above."""
        for path, status in self.results["allowed"]:
            with self.subTest(path=path):
                self.assertEqual(status, 200)

    def test_a_request_with_neither_fetch_metadata_nor_origin_is_refused(self) -> None:
        """🔴 The fail-open this check exists to avoid.

        Both headers are "meaningful only when present", so "check
        Sec-Fetch-Site, else check Origin, else allow" admits anything that omits
        both. Refusing that case turns away every page-driven cross-origin
        request, which is the class CSRF is about, and costs nothing: a browser
        always sends Origin on a same-origin POST. It does not stop a caller that
        composes its own request - that one can send Origin too - but such a
        caller already holds the session cookie and is a session-theft problem,
        not a CSRF one.
        """
        self.assertEqual(self.results["headerless_post"], 403)

    def test_an_origin_header_alone_is_enough(self) -> None:
        """Mutation anchor: a check that refuses everything also passes the two above."""
        self.assertEqual(self.results["origin_only_post"], 200)

    def test_only_the_origin_bearing_request_reached_grafana(self) -> None:
        self.assertEqual(self.results["csrf_reached_grafana"], 0)

    def test_the_declared_datasource_is_forwarded(self) -> None:
        """Mutation anchor for every datasource refusal below."""
        self.assertEqual(self.results["ds_declared"], 200)

    def test_another_datasource_is_refused(self) -> None:
        """/api/ds/query dispatches on a uid in the body, so the URL allowlist cannot bound it.

        An operator whose Grafana also has a SQL datasource attached would
        otherwise have a same-origin channel for arbitrary SQL, and a
        folder-scoped Viewer does not stop it: OSS Grafana does not scope
        datasource permissions by folder.
        """
        self.assertEqual(self.results["ds_other"], 403)

    def test_one_out_of_bounds_query_refuses_the_whole_request(self) -> None:
        """Forwarding the rest would still have run the one that mattered."""
        self.assertEqual(self.results["ds_mixed"], 403)

    def test_every_uncertain_body_is_refused(self) -> None:
        """Unparseable, empty, unnamed and legacy-addressed bodies all fail closed.

        A query with no datasource is the subtle one: Grafana falls back to its
        default datasource, which is whatever the operator made default - not
        something this proxy has vetted.
        """
        for name, status in self.results["ds_uncertain"].items():
            with self.subTest(case=name):
                self.assertEqual(status, 403)

    def test_only_the_accepted_datasource_body_travelled(self) -> None:
        self.assertEqual(self.results["ds_reached_grafana"], 1)

    def test_an_unnamed_datasource_counts_as_unconfigured(self) -> None:
        """Without it the proxy cannot bound what /api/ds/query may reach.

        Refusing to enable is the honest outcome: the alternative is a panel
        that renders while forwarding queries nobody scoped.
        """
        self.assertEqual(self.results["no_datasource_admin"], 404)

    def test_the_one_write_shaped_route_requires_same_origin(self) -> None:
        """POST /api/ds/query stands in for the CSRF token it cannot carry.

        Grafana's own front-end issues it and cannot read the Console's
        double-submit cookie, so fetch metadata replaces that check. Without
        this the single non-GET entry would have no CSRF defence at all.
        """
        self.assertEqual(self.results["cross_site_post"], 403)

    def test_the_console_session_cookie_never_reaches_grafana(self) -> None:
        upstream = self.results["upstream"]
        self.assertIsNotNone(upstream)
        self.assertNotIn("cookie", upstream["headers"])

    def test_only_the_service_account_token_goes_upstream(self) -> None:
        upstream = self.results["upstream"]
        self.assertEqual(
            upstream["headers"]["authorization"], "Bearer grafana-sa-token"
        )
        self.assertNotIn("test-admin-token", upstream["headers"]["authorization"])

    def test_grafana_set_cookie_never_reaches_the_browser(self) -> None:
        headers = self.results["admin_headers"]
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn(
            "x-grafana-internal", headers,
            "response headers are an allowlist; unknown upstream headers stay upstream",
        )

    def test_the_panel_response_may_be_framed_by_this_origin_only(self) -> None:
        policy = self.results["admin_headers"]["content-security-policy"]
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertNotIn("frame-ancestors 'none'", policy)
        self.assertEqual(self.results["admin_headers"]["x-frame-options"], "SAMEORIGIN")
        self.assertIn("object-src 'none'", policy)
        self.assertIn("form-action 'none'", policy)

    def test_an_unconfigured_deployment_has_no_such_route(self) -> None:
        self.assertEqual(self.results["unconfigured_admin"], 404)

    def test_half_configured_counts_as_unconfigured(self) -> None:
        """A base URL with no token would fill the iframe with 401s: worse than an absent tab."""
        self.assertEqual(self.results["half_configured_admin"], 404)

    def test_whoami_tells_the_console_whether_to_render_the_tab(self) -> None:
        self.assertEqual(self.results["unconfigured_whoami"], {"enabled": False})
        configured = self.results["whoami_configured"]
        self.assertTrue(configured["enabled"])
        self.assertEqual(configured["dashboardUid"], "sandbox-control-plane")
        self.assertTrue(configured["panels"])

    def test_a_tenant_is_not_told_the_panel_exists(self) -> None:
        self.assertIsNone(self.results["whoami_tenant"])


class PanelCatalogTests(unittest.TestCase):
    """The catalog and the dashboard are two spellings of one fact.

    A panel id that no longer exists renders as an empty rectangle: Grafana
    answers 200 with nothing in it, so no status code, log line or metric says
    anything is wrong. This is the only place the two can be compared.
    """

    def test_every_offered_panel_exists_in_the_shipped_dashboard(self) -> None:
        sys.path.insert(0, str(ROOT))
        from control_plane import grafana_proxy
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], grafana_proxy.DEFAULT_DASHBOARD_UID)
        available = {panel["id"]: panel["title"] for panel in dashboard["panels"]}
        # Self-check: an empty dashboard would let every catalog entry through.
        self.assertTrue(available)
        for panel_id, title in grafana_proxy.PANELS:
            with self.subTest(panel=panel_id):
                self.assertIn(panel_id, available)
                self.assertEqual(available[panel_id], title)

    def test_the_dashboard_uses_exactly_one_datasource_variable(self) -> None:
        """``allowed_datasource_uids`` returns one uid because the dashboard needs one.

        If a future panel pins its own datasource, that assumption stops holding
        and the proxy would start refusing a query the panel legitimately makes -
        or, worse, someone would widen the allowlist to match. Catch it here.
        """
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        variables = [
            item for item in dashboard["templating"]["list"]
            if item.get("type") == "datasource"
        ]
        self.assertEqual(len(variables), 1)
        for panel in dashboard["panels"]:
            with self.subTest(panel=panel["title"]):
                source = panel.get("datasource") or {}
                # The variable reference, never a pinned uid.
                self.assertEqual(source.get("uid"), "${datasource}")


class NginxContractTests(unittest.TestCase):
    """The panel needs a route through the Console's Nginx, and one policy, not two.

    Nothing at runtime joins console/nginx.conf to control_plane/grafana_proxy.py. If
    the location block disappears the panel path falls through to the static
    fallback and the iframe silently renders index.html; if the two policies
    drift the browser blanks the panel. Both fail quietly, so they are asserted
    here.
    """

    NGINX = ROOT / "console" / "nginx.conf"

    def _block(self) -> str:
        text = self.NGINX.read_text(encoding="utf-8")
        start = text.index("location ^~ /grafana/ {")
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        raise AssertionError("unterminated /grafana/ location block")

    def test_the_panel_path_reaches_control_plane_rather_than_the_static_fallback(self) -> None:
        block = self._block()
        self.assertIn("proxy_pass http://$sandbox_control_plane_upstream;", block)
        self.assertIn("location ^~ /grafana/ {", block)
        # The session cookie is the credential Control Plane authenticates; an iframe
        # cannot send anything else.
        self.assertIn("proxy_set_header Cookie $http_cookie;", block)

    def test_the_framing_policy_matches_the_one_control_plane_sends(self) -> None:
        sys.path.insert(0, str(ROOT))
        from control_plane import grafana_proxy
        block = self._block()
        self.assertIn(
            f'add_header Content-Security-Policy "{grafana_proxy.PANEL_CSP}" always;',
            block,
        )
        self.assertIn('add_header X-Frame-Options "SAMEORIGIN" always;', block)
        # Both upstream copies are hidden, or the browser receives two policies
        # and intersects them into an unusable one.
        self.assertIn("proxy_hide_header Content-Security-Policy;", block)
        self.assertIn("proxy_hide_header X-Frame-Options;", block)

    def test_the_console_keeps_its_own_policy_outside_that_location(self) -> None:
        """Acceptance criterion: adding the panel must not relax the Console CSP."""
        text = self.NGINX.read_text(encoding="utf-8")
        server_level = text[: text.index("location ^~ /grafana/ {")]
        self.assertIn("frame-ancestors 'none'", server_level)
        self.assertIn("default-src 'self'", server_level)
        self.assertIn('add_header X-Frame-Options "DENY" always;', server_level)
        directives = [
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # frame-src is what a cross-origin iframe would have needed. Its absence
        # from every directive is the whole point of proxying on this origin.
        self.assertNotIn("frame-src", "\n".join(directives))


class ConsoleIntegrationTests(unittest.TestCase):
    """The console builds the panel URL; the proxy decides which URLs exist.

    Nothing at runtime joins them. If the allowlist regex is tightened or the
    route prefix renamed, the console keeps rendering iframes and every one of
    them answers 403 - a grid of empty rectangles with a status code nobody is
    looking at. Compared here because this is the only place both spellings are
    visible at once.
    """

    VIEW = ROOT / "console" / "src" / "components" / "ObservabilityView.tsx"

    def setUp(self) -> None:
        self.source = self.VIEW.read_text(encoding="utf-8")

    def test_the_route_prefix_the_console_falls_back_to_is_the_one_served(self) -> None:
        sys.path.insert(0, str(ROOT))
        from control_plane import grafana_proxy
        self.assertIn(f'grafana.route ?? "{grafana_proxy.ROUTE_PREFIX}"', self.source)

    def test_the_url_the_console_builds_is_on_the_allowlist(self) -> None:
        sys.path.insert(0, str(ROOT))
        from control_plane import grafana_proxy
        # The console renders `${route}d-solo/${uid}?...`; strip the prefix the
        # same way the proxy does and ask the allowlist directly.
        self.assertIn("d-solo/${encodeURIComponent(uid)}", self.source)
        built = f"{grafana_proxy.ROUTE_PREFIX}d-solo/sandbox-control-plane"
        upstream = grafana_proxy.upstream_path(built)
        self.assertIsNotNone(upstream)
        self.assertTrue(grafana_proxy.is_allowed("GET", upstream))

    def test_the_console_never_names_a_grafana_address(self) -> None:
        """Same-origin is the whole design; an address here would undo it.

        The frame src must start from the proxy route, not from anything the
        browser could resolve on its own - the moment a Grafana host appears in
        this file, the CSP and the service-account token both stop mattering.

        🔴 The assertion is about address *shape*, not about the word "grafana"
        appearing. The first version of this guard searched for the bare
        substring ``grafana:`` and was promptly hit by this component's own
        TypeScript annotation, ``grafana: GrafanaCapability`` - a guard that
        fires on the legitimate spelling of the thing it guards. Two shapes are
        checked instead: an absolute URL, and a bare ``host:port``.
        ``test_a_real_address_would_be_caught`` proves the tightening did not
        turn a false positive into a blind spot, which is the failure mode a
        regex fix runs into next and which looks exactly like passing.
        """
        for marker in ("http://", "https://", "//grafana", "VITE_GRAFANA"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.source)
        self.assertIsNone(
            HOST_PORT.search(self.source),
            "a bare host:port would resolve without going through the proxy",
        )
        self.assertIn("src={`${route}d-solo/", self.source)

    def test_a_real_address_would_be_caught(self) -> None:
        """Self-check on the regex above: from over-matching to never matching is one character."""
        for sample in (
            'src={`http://grafana.example/d-solo/`}',
            'const host = "grafana:3000";',
            'const host = "grafana.internal:3000";',
        ):
            with self.subTest(sample=sample):
                self.assertTrue(
                    sample.startswith("src={`http")
                    or HOST_PORT.search(sample) is not None
                )
        # ...and the legitimate spelling that broke the first version does not.
        self.assertIsNone(HOST_PORT.search("  grafana: GrafanaCapability;"))

    def test_the_panel_frame_is_sandboxed_to_what_a_chart_needs(self) -> None:
        self.assertIn('sandbox="allow-scripts allow-same-origin"', self.source)
        for token in ("allow-top-navigation", "allow-popups", "allow-forms"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.source)


class TracePropagationTests(unittest.TestCase):
    """The Grafana hop carries W3C trace context, and mints its own span for it.

    Correlation is the only thing that lets "an administrator opened a panel"
    and "Grafana took nine seconds" be recognised as one event later. There is
    no tracing SDK here and none is wanted; the header is a string.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = PanelProxyTests.results
        sys.path.insert(0, str(ROOT))
        from control_plane import grafana_proxy
        cls.proxy = grafana_proxy

    def parsed(self, key: str):
        value = self.results[key]
        self.assertIsNotNone(value, f"{key}: no traceparent reached Grafana")
        parsed = self.proxy.parse_traceparent(value)
        self.assertIsNotNone(parsed, f"{key}: malformed traceparent {value!r}")
        return parsed

    def test_a_request_with_no_trace_still_gets_one(self) -> None:
        # We minted it, so we decide: sampled.
        self.assertEqual(self.parsed("trace_none")[1], "01")

    def test_an_inbound_trace_is_continued_under_a_new_span(self) -> None:
        trace, _flags = self.parsed("trace_inherit_sampled")
        self.assertEqual(trace, self.results["inbound_trace"])
        self.assertNotIn(
            "00f067aa0ba902b7", self.results["trace_inherit_sampled"],
            "the caller's span id was reused",
        )

    def test_an_inbound_sampling_decision_is_not_overturned(self) -> None:
        """🔴 `00` means "do not sample". Upgrading it would be a silent override.

        Nothing fails when a propagator flips this: the trace stays connected
        and the only symptom is a collector receiving a stretch of spans it was
        told to skip. That makes it exactly the kind of decision that has to be
        asserted rather than trusted.
        """
        self.assertEqual(self.parsed("trace_inherit_unsampled")[1], "00")

    def test_the_header_is_matched_without_regard_to_case(self) -> None:
        """The wire spelling is not a contract; matching case-insensitively is."""
        self.assertEqual(
            self.parsed("trace_mixed_case")[0], self.results["inbound_trace"]
        )

    def test_a_request_id_already_shaped_like_a_trace_is_adopted(self) -> None:
        self.assertEqual(
            self.parsed("trace_from_request_id")[0], self.results["inbound_trace"]
        )

    def test_a_malformed_inbound_trace_is_replaced_rather_than_relayed(self) -> None:
        """Relaying a broken header would poison the trace it lands in."""
        self.parsed("trace_malformed")
        self.assertNotIn("not-a-trace", self.results["trace_malformed"])


if __name__ == "__main__":
    unittest.main()
