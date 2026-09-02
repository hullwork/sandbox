"""The observability artifacts must agree with the metrics core.py registers.

There is no runtime step that joins an alert rule to the code that emits the
metric it names.  A misspelled or removed metric does not error anywhere: the
expression evaluates to an empty vector forever, Prometheus shows the rule as
healthy, and the control_plane is "monitored" with zero coverage on that signal.  This
file is the only place the two spellings meet.

Deliberately a private copy rather than something shared with a sibling
repository: these repositories ship and may be operated separately, so a shared
test helper would be a dependency in exactly the direction being removed.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "observability"
ALERTS = OBSERVABILITY / "alerts" / "sandbox-control-plane-rules.yaml"
SCRAPE = OBSERVABILITY / "scrape" / "servicemonitor.yaml"
DASHBOARD = OBSERVABILITY / "dashboards" / "sandbox-control-plane.json"

_METRIC_NAME = re.compile(r"\bsandbox_[a-z0-9_]+")
_REGISTRATION = re.compile(
    r"metrics_lib\.(?:Counter|Gauge|Histogram)\(\s*\"(sandbox_[a-z0-9_]+)\""
)
# Labels an annotation or expression may name.  Every one is a closed
# enumeration written by core.py (create_failure_reason, the three quota
# gates, the reaper/credential kinds) or added by the scraper.  Anything else
# would be a route for a tenant, workspace or sandbox id to reach a series or an
# alert body - and /metrics is unauthenticated.
_ALLOWED_LABELS = {
    "reason", "gate", "kind", "job", "instance", "method", "route",
    "status_class", "phase", "operation", "outcome",
}
_FORBIDDEN_LABELS = ("tenant", "workspace", "subject", "sandbox_id", "owner", "user")


def _registered_metric_names() -> set[str]:
    source = (ROOT / "control_plane" / "core.py").read_text(encoding="utf-8")
    names = set(_REGISTRATION.findall(source))
    # A histogram is exposed as the _bucket/_sum/_count trio, never under the
    # bare name, so rules may legitimately reference those spellings.
    for name in list(names):
        names.update({f"{name}_bucket", f"{name}_sum", f"{name}_count"})
    return names


class AlertRuleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(ALERTS.exists(), f"{ALERTS} is missing")
        document = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
        self.assertEqual(document["kind"], "PrometheusRule")
        self.groups = document["spec"]["groups"]
        self.rules = [
            rule for group in self.groups for rule in group["rules"] if "alert" in rule
        ]

    def test_every_rule_is_complete_enough_to_act_on(self) -> None:
        self.assertTrue(self.rules, "no alert rules at all")
        seen = set()
        for rule in self.rules:
            with self.subTest(alert=rule["alert"]):
                self.assertNotIn(rule["alert"], seen, "duplicate alert name")
                seen.add(rule["alert"])
                self.assertTrue(str(rule.get("expr", "")).strip())
                self.assertIn("for", rule)
                self.assertIn(rule["labels"]["severity"], {"critical", "warning"})
                self.assertTrue(rule["annotations"]["summary"])
                self.assertTrue(rule["annotations"]["description"])

    def test_expressions_only_name_metrics_control_plane_registers(self) -> None:
        registered = _registered_metric_names()
        # Self-check: an empty scan would let every expression through.
        self.assertIn("sandbox_runtimes_live", registered)
        seen: set[str] = set()
        for rule in self.rules:
            used = set(_METRIC_NAME.findall(rule["expr"]))
            seen |= used
            with self.subTest(alert=rule["alert"]):
                self.assertLessEqual(
                    used,
                    registered,
                    f"unregistered metric names: {sorted(used - registered)}",
                )
        # Self-check: a regex matching nothing would pass every rule.
        self.assertTrue(seen)

    def test_no_high_cardinality_or_tenant_label_reaches_an_alert(self) -> None:
        """/metrics is unauthenticated; an alert body must not leak what the series does not carry."""
        for rule in self.rules:
            with self.subTest(alert=rule["alert"]):
                text = rule["expr"] + " " + " ".join(rule["annotations"].values())
                used = set(re.findall(r"\$labels\.([a-z_]+)", text))
                self.assertLessEqual(used, _ALLOWED_LABELS, text)
                for forbidden in _FORBIDDEN_LABELS:
                    self.assertNotIn(
                        f"{forbidden}=", rule["expr"],
                        f"{rule['alert']} selects on a label core.py does not emit",
                    )

    def test_rule_job_regex_matches_the_shipped_scrape_job(self) -> None:
        """A rule keyed on a job name nobody scrapes has no `up` series and can never fire."""
        self.assertTrue(SCRAPE.exists(), f"{SCRAPE} is missing")
        documents = [d for d in yaml.safe_load_all(SCRAPE.read_text()) if d]
        monitor = next(d for d in documents if d["kind"] == "ServiceMonitor")
        # The Prometheus Operator derives `job` from the Service name, which is
        # the label the ServiceMonitor selects on.
        job = monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"]
        sample = next(d for d in documents if d["kind"] == "ConfigMap")
        static_jobs = {
            entry["job_name"]
            for entry in yaml.safe_load(sample["data"]["scrape_configs.yml"])[
                "scrape_configs"
            ]
        }
        for rule in self.rules:
            match = re.search(r'job=~"([^"]+)"', rule["expr"])
            if match is None:
                continue
            pattern = match.group(1)
            with self.subTest(alert=rule["alert"]):
                self.assertRegex(job, f"^(?:{pattern})$")
                self.assertTrue(
                    [name for name in static_jobs if re.fullmatch(pattern, name)],
                    f"no static scrape job matches {pattern!r}",
                )


class DashboardContractTest(unittest.TestCase):
    """The dashboard is shipped for import, so its queries answer to the same rule."""

    def test_dashboard_only_plots_metrics_control_plane_registers(self) -> None:
        self.assertTrue(DASHBOARD.exists(), f"{DASHBOARD} is missing")
        import json

        registered = _registered_metric_names()
        body = DASHBOARD.read_text(encoding="utf-8")
        json.loads(body)  # a panel file that does not parse imports as nothing
        used = set(_METRIC_NAME.findall(body))
        self.assertTrue(used, "dashboard plots no sandbox_* metric at all")
        self.assertLessEqual(
            used, registered, f"unregistered metric names: {sorted(used - registered)}"
        )


class CounterZeroRegistrationTests(unittest.TestCase):
    """"Never failed" and "this build has no such metric" must not look alike.

    A counter only appears once something increments it, so on the scraper side
    both are an absent series. `rate(..._failures_total[10m]) > 0` cannot tell
    them apart and `absent(...)` misfires on the instance that genuinely never
    failed - the alert is healthy-looking in both directions, which is the worst
    shape a guard can have.
    """

    def _metrics_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_control_plane_metrics_under_test", ROOT / "control_plane" / "metrics.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_an_unregistered_counter_renders_nothing(self) -> None:
        """The behaviour ensure() exists to fix; asserted so the fix has a baseline."""
        metrics = self._metrics_module()
        counter = metrics.Counter("sandbox_example_total", "help")
        self.assertEqual(counter.render(), [])

    def test_ensure_makes_zero_an_observation(self) -> None:
        metrics = self._metrics_module()
        counter = metrics.Counter("sandbox_example_total", "help")
        counter.ensure(reason="quota")
        rendered = counter.render()
        self.assertIn('sandbox_example_total{reason="quota"} 0', rendered)

    def test_ensure_never_overwrites_a_counted_value(self) -> None:
        metrics = self._metrics_module()
        counter = metrics.Counter("sandbox_example_total", "help")
        counter.inc(3, reason="quota")
        counter.ensure(reason="quota")
        self.assertIn('sandbox_example_total{reason="quota"} 3', counter.render())

    def test_the_control_plane_registers_its_known_label_combinations(self) -> None:
        """Otherwise the alerts in observability/alerts/ cannot see a healthy Control Plane.

        Read from the module source rather than by importing core.py, which
        needs a cluster and a store to import at all.
        """
        source = (ROOT / "control_plane" / "core.py").read_text(encoding="utf-8")
        for expected in (
            'RUNTIME_CREATE_FAILURES.ensure(reason=_reason)',
            'QUOTA_REJECTIONS.ensure(gate=_gate)',
            'AUDIT_FAILURES.ensure()',
            'STORE_ERRORS.ensure()',
            'OBJECT_TICKET_FAILURES.ensure(reason=_reason)',
        ):
            with self.subTest(call=expected):
                self.assertIn(expected, source)


class LabelledHistogramTests(unittest.TestCase):
    def test_each_label_set_has_independent_cumulative_buckets(self) -> None:
        from control_plane import metrics

        histogram = metrics.Histogram("sandbox_latency_seconds", "help", (1, 5))
        histogram.observe(0.5, route="fast")
        histogram.observe(3, route="slow")
        rendered = histogram.render()
        self.assertIn(
            'sandbox_latency_seconds_bucket{le="1",route="fast"} 1', rendered
        )
        self.assertIn(
            'sandbox_latency_seconds_bucket{le="1",route="slow"} 0', rendered
        )
        self.assertIn('sandbox_latency_seconds_count{route="fast"} 1', rendered)
        self.assertIn('sandbox_latency_seconds_count{route="slow"} 1', rendered)


class HealthEndpointDisclosureTests(unittest.TestCase):
    """/healthz is unauthenticated, so its body is readable by anything that can reach the port.

    The address of the thing that failed is topology, not diagnosis. The
    classification is what an operator acts on and it stays in the response; the
    address moves to the process log, where reading it already needs cluster
    access. Verified end to end rather than by reading the source, because the
    thing being asserted is what a stranger receives.
    """

    PROBE = textwrap.dedent(
        """
        import json
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from control_plane import kube
        class FakeKube:
            def __init__(self):
                pass

            def list(self, *args, **kwargs):
                return []

            def get(self, *args, **kwargs):
                return {}

        kube.KubeClient = FakeKube

        from control_plane import api
        from control_plane import core as control_plane
        SECRET_HOST = "object-store.storage-system.svc.cluster.local"

        def exploding_urlopen(url, timeout=None):
            raise urllib.error.URLError(
                f"[Errno -2] Name or service not known: {url}"
            )

        control_plane.urlopen = exploding_urlopen
        control_plane.OBJECT_STORE_ENDPOINT = f"http://{SECRET_HOST}:9000"
        control_plane.OBJECT_STORE_HEALTH_PATH = "/minio/health/ready"

        server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/healthz" % server.server_port
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
        finally:
            server.shutdown()
            server.server_close()
        print(json.dumps({
            "status": status,
            "body": raw.decode("utf-8", "replace"),
            "secret_host": SECRET_HOST,
        }))
        """
    )

    @classmethod
    def setUpClass(cls) -> None:
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
            "SIGNING_KEY": "0" * 32,
            "WORKSPACE_ID_KEY": "1" * 32,
            "VOLUME_AGENT_TOKEN": "test-volume-token",
            "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
            "OBJECT_STORE_ACCESS_KEY": "test-access",
            "OBJECT_STORE_SECRET_KEY": "test-secret",
            "PYTHONPATH": str(ROOT),
        }
        environment.pop("SANDBOX_CONTROL_PLANE_ROLE", None)
        result = subprocess.run(
            [sys.executable, "-c", cls.PROBE],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        cls.result = json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_probe_actually_reached_the_storage_failure_branch(self) -> None:
        """Self-check: a 200 would make every assertion below vacuous."""
        self.assertEqual(self.result["status"], 503)

    def test_the_object_store_address_is_not_in_the_response(self) -> None:
        self.assertNotIn(self.result["secret_host"], self.result["body"])
        body = json.loads(self.result["body"])
        self.assertNotIn("endpoint", body)
        self.assertNotIn("health_path", body)

    def test_the_actionable_classification_survives(self) -> None:
        """Redacting must not cost the operator the three-way distinction.

        "Cannot resolve the name" and "resolved but refused" need completely
        different next actions, and that verdict is the reason this branch has a
        body at all.
        """
        body = json.loads(self.result["body"])
        self.assertIn("cannot be resolved", body["diagnosis"])
        self.assertIn("object storage unavailable", body["error"])
        self.assertIn("<redacted>", body["error"])


if __name__ == "__main__":
    unittest.main()
