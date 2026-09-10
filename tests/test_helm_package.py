from __future__ import annotations

import json
import hashlib
import pathlib
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "sandbox"


class HelmPackageContractTests(unittest.TestCase):
    def test_chart_acknowledges_every_canonical_manifest_revision(self) -> None:
        recorded = {}
        for line in (CHART / "source-manifests.sha256").read_text().splitlines():
            digest, relative = line.split(maxsplit=1)
            recorded[relative] = digest
        current = {}
        for path in sorted((ROOT / "k8s").rglob("*.yaml")):
            relative = path.relative_to(ROOT).as_posix()
            current[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(current, recorded, "canonical manifests changed; update the Helm templates and checksum gate together")

    def test_chart_is_product_owned_and_standalone(self) -> None:
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        # package.yaml was a Package descriptor for a separate infrastructure
        # repository's composition tooling. This chart is published on its own,
        # so the descriptor and its release-time checksum are gone.
        self.assertFalse((CHART / "package.yaml").exists())
        self.assertNotIn(
            "package.yaml",
            (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
        )
        self.assertEqual(chart["name"], "sandbox")
        self.assertEqual(chart["version"], project["project"]["version"])
        self.assertEqual(chart["appVersion"], project["project"]["version"])
        # The chart is the only place the licence is restated for a registry
        # audience, so it is the one copy nobody rereads. It shipped as
        # Apache-2.0 against three MIT declarations until this assertion existed.
        self.assertEqual("MIT", chart["annotations"]["artifacthub.io/license"])
        self.assertEqual("MIT", project["project"]["license"])
        self.assertEqual("MIT License", (ROOT / "LICENSE").read_text().splitlines()[0])

    def test_values_schema_supports_digest_pinned_images(self) -> None:
        schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
        image = schema["$defs"]["image"]["properties"]
        image_with_policy = schema["$defs"]["imageWithPolicy"]["properties"]
        self.assertIn("digest", image)
        self.assertIn("digest", image_with_policy)
        self.assertIn("sha256", image["digest"]["pattern"])

    def test_the_documented_restart_set_matches_what_mounts_the_secrets(self) -> None:
        """Every rendered workload that mounts a pre-provisioned Secret is named.

        The chart owns both namespaces and creates none of the four Secrets, so
        `helm install` has to run before they can exist and the workloads that
        want them come up first and fail. The install-order section is the only
        place that says which ones to restart; a workload added later that also
        mounts one of these Secrets would silently stay broken.
        """
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        rendered = subprocess.run(
            ["helm", "template", "sandbox", str(CHART)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        wanted = {
            "sandbox-api-credentials",
            "sandbox-postgres-auth",
            "object-store-credentials",
            "sandbox-volume-auth",
        }
        consumers = set()
        for document in yaml.safe_load_all(rendered):
            if not document or document.get("kind") not in {
                "Deployment", "StatefulSet", "Job", "CronJob",
            }:
                continue
            serialised = yaml.safe_dump(document)
            if any(name in serialised for name in wanted):
                consumers.add(document["metadata"]["name"])
        self.assertTrue(consumers, "no rendered workload mounts a pre-provisioned Secret")
        guide = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
        start = guide.index("### Install order")
        section = guide[start:guide.index("\nThis is a multi-namespace package", start)]
        missing = sorted(name for name in consumers if name not in section)
        self.assertEqual(
            missing, [], f"install order does not say to restart: {missing}"
        )

    def test_embedded_postgres_accepts_only_control_plane_ingress(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        rendered = subprocess.run(
            ["helm", "template", "sandbox", str(CHART)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [item for item in yaml.safe_load_all(rendered) if item]
        policy = next(
            item for item in documents
            if item.get("kind") == "NetworkPolicy"
            and item["metadata"]["name"] == "sandbox-postgres-ingress"
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "sandbox-postgres"},
        )
        self.assertEqual(policy["spec"]["ingress"], [{
            "from": [{"podSelector": {"matchLabels": {
                "app.kubernetes.io/name": "sandbox-control-plane",
            }}}],
            "ports": [{"protocol": "TCP", "port": 5432}],
        }])

        statefulset = next(
            item for item in documents
            if item.get("kind") == "StatefulSet"
            and item["metadata"]["name"] == "sandbox-postgres"
        )
        environment = {
            item["name"]: item.get("value")
            for item in statefulset["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(environment["PGDATA"], "/var/lib/postgresql/data/pgdata")

    def test_postgres_secret_override_reaches_every_consumer(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        secret = "operator-managed-postgres"
        rendered = subprocess.run(
            [
                "helm", "template", "sandbox", str(CHART),
                "--set-string", f"postgresql.authSecret={secret}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertNotIn("sandbox-postgres-auth", rendered)
        self.assertEqual(rendered.count(f"name: {secret}"), 5)
        self.assertIn(f"secretName: {secret}", rendered)

    def test_external_postgres_replaces_the_embedded_database(self) -> None:
        """Switching off the embedded server must move the address too.

        The chart already omitted the Service and the StatefulSet, but the
        Control Plane kept a hardcoded in-cluster host and no port of its own:
        a release built for an external server dialled a Service the same
        render had declined to create.
        """
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        host = "postgres.example.net"
        rendered = subprocess.run(
            [
                "helm", "template", "sandbox", str(CHART),
                "--set", "postgresql.embedded.enabled=false",
                "--set-string", f"postgresql.external.host={host}",
                "--set", "postgresql.external.port=6432",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [item for item in yaml.safe_load_all(rendered) if item]
        named = {(item.get("kind"), item["metadata"]["name"]) for item in documents}
        for absent in ("Service", "StatefulSet", "NetworkPolicy"):
            self.assertNotIn((absent, "sandbox-postgres"), named)
        control_plane = next(
            item for item in documents
            if item.get("kind") == "Deployment"
            and item["metadata"]["name"] == "sandbox-control-plane"
        )
        environment = {
            item["name"]: item.get("value")
            for item in control_plane["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(host, environment["SANDBOX_DB_HOST"])
        # A string, not an integer: the API server refuses a non-string env value.
        self.assertEqual("6432", environment["SANDBOX_DB_PORT"])

    def test_external_postgres_without_a_host_is_refused_at_render(self) -> None:
        """The Control Plane's own default is the reason this cannot be silent.

        `SANDBOX_DB_HOST` falls back to `sandbox-postgres` in the process, so an
        empty external host would ship a release that starts, reports ready, and
        answers every request from a Service that does not exist.
        """
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        result = subprocess.run(
            [
                "helm", "template", "sandbox", str(CHART),
                "--set", "postgresql.embedded.enabled=false",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("postgresql.external.host", result.stderr)

    def test_otlp_endpoint_is_opt_in_and_reaches_both_traced_roles(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        endpoint = "http://otel-collector.observability.svc:4318/v1/traces"
        rendered = subprocess.run(
            [
                "helm", "template", "sandbox", str(CHART),
                "--set-string", f"observability.tracing.endpoint={endpoint}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [item for item in yaml.safe_load_all(rendered) if item]
        deployments = {
            item["metadata"]["name"]: item
            for item in documents if item.get("kind") == "Deployment"
        }
        for name in ("sandbox-control-plane", "sandbox-volume"):
            env = deployments[name]["spec"]["template"]["spec"]["containers"][0]["env"]
            values = {item["name"]: item.get("value") for item in env}
            self.assertEqual(values["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], endpoint)
            self.assertEqual(values["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"], "http/json")

    def test_system_and_runtime_scheduling_are_separate_public_values(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        rendered = subprocess.run(
            [
                "helm", "template", "sandbox", str(CHART),
                "--set-string", "scheduling.system.nodeSelector.example\\.com/node-role=system",
                "--set-string", "runtime.nodeSelector.sandbox\\.hullwork\\.com/node-role=runtime",
                "--set-string", "runtime.tolerations[0].key=sandbox.hullwork.com/node-role",
                "--set-string", "runtime.tolerations[0].operator=Equal",
                "--set-string", "runtime.tolerations[0].value=runtime",
                "--set-string", "runtime.tolerations[0].effect=NoSchedule",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [item for item in yaml.safe_load_all(rendered) if item]
        workloads = [
            item for item in documents
            if item.get("kind") in {"Deployment", "StatefulSet", "Job", "CronJob"}
        ]
        for item in workloads:
            spec = item["spec"]
            if item["kind"] == "CronJob":
                pod = spec["jobTemplate"]["spec"]["template"]["spec"]
            else:
                pod = spec["template"]["spec"]
            self.assertEqual(
                pod["nodeSelector"], {"example.com/node-role": "system"},
                item["metadata"]["name"],
            )
        control_plane = next(
            item for item in workloads
            if item["metadata"]["name"] == "sandbox-control-plane"
        )
        env = {
            item["name"]: item.get("value")
            for item in control_plane["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(
            env["SANDBOX_RUNTIME_NODE_SELECTOR"],
            "sandbox.hullwork.com/node-role=runtime",
        )
        self.assertEqual(
            json.loads(env["SANDBOX_RUNTIME_TOLERATIONS"])[0]["effect"],
            "NoSchedule",
        )

    def test_release_publishes_oci_chart_after_ci_gate(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("helm-chart:", workflow)
        self.assertIn("helm push", workflow)
        self.assertIn("oci://ghcr.io/", workflow)
        self.assertIn("needs: [release-context, images]", workflow)
        self.assertIn("package-metadata.json", workflow)
        self.assertIn("schemaVersion:1", workflow)
        self.assertIn("immutableRef", workflow)
        self.assertIn("runtimeImages", workflow)
        self.assertIn("valuePath", workflow)

    def test_chart_renders_after_copy_without_sibling_repositories(self) -> None:
        if not shutil.which("helm"):
            self.skipTest("helm is not installed")
        with tempfile.TemporaryDirectory() as directory:
            copied = pathlib.Path(directory) / "chart"
            shutil.copytree(CHART, copied)
            subprocess.run(
                ["helm", "template", "sandbox", str(copied)],
                check=True,
                stdout=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    unittest.main()
