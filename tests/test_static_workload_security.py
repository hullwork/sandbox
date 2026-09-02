"""Security context of every static workload, read back from each render path.

tests/test_manifests_security.py pins the Runtime Pod that manifests.py
generates. The six Deployments, Jobs and CronJobs under k8s/ and overlays/,
and the chart's postgres StatefulSet, had no reader at all: setting
readOnlyRootFilesystem to false in k8s/control-plane.yaml left the suite
green, and test_helm_package's checksum gate only says "k8s/ changed", not
"this field changed". Every workload each path renders is checked here.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KUSTOMIZE_PATHS = (
    "k8s",
    "overlays/rwo-single-node",
    "overlays/local",
    "overlays/eks",
    "overlays/external-deps",
)
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job", "CronJob", "DaemonSet"}

# Exceptions are named so the test is exact in both directions: a workload
# that later gains the field fails until it is removed from here.
# postgres writes its socket and PID under /var/run/postgresql and the
# upstream image expects a writable root; see charts/sandbox/templates/postgres.yaml.
WRITABLE_ROOT = {"sandbox-postgres"}
# Control Plane creates Runtime Pods through the API and needs its token;
# metrics-server is the vendored upstream manifest in kube-system.
MOUNTS_SERVICE_ACCOUNT_TOKEN = {"sandbox-control-plane", "metrics-server"}
# What the base must contain; an overlay that dropped one would otherwise
# shrink the checked set without anything noticing.
BASE_WORKLOADS = {
    "sandbox-control-plane",
    "sandbox-volume",
    "sandbox-console",
    "sandbox-workspace-gc",
    "sandbox-workspace-init",
}


def pod_spec(document: dict) -> dict:
    spec = document["spec"]
    if document["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    return spec["template"]["spec"]


def workloads(rendered: str) -> dict[str, dict]:
    return {
        document["metadata"]["name"]: document
        for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") in WORKLOAD_KINDS
    }


def renders() -> dict[str, dict[str, dict]]:
    found: dict[str, dict[str, dict]] = {}
    if shutil.which("kubectl"):
        for relative in KUSTOMIZE_PATHS:
            output = subprocess.run(
                ["kubectl", "kustomize", str(ROOT / relative)],
                capture_output=True, text=True, check=True,
            ).stdout
            found[relative] = workloads(output)
    if shutil.which("helm"):
        output = subprocess.run(
            ["helm", "template", "sandbox", str(ROOT / "charts" / "sandbox")],
            capture_output=True, text=True, check=True,
        ).stdout
        found["charts/sandbox"] = workloads(output)
    return found


class StaticWorkloadSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.renders = renders()
        if not cls.renders:
            raise unittest.SkipTest("neither kubectl nor helm is installed")

    def every_workload(self):
        for source, found in self.renders.items():
            self.assertTrue(found, f"{source} rendered no workloads")
            for name, document in found.items():
                yield f"{source}:{name}", name, document

    def test_the_base_renders_every_expected_workload(self) -> None:
        for source, found in self.renders.items():
            with self.subTest(render=source):
                self.assertTrue(BASE_WORKLOADS <= set(found), set(found))

    def test_pods_run_as_non_root_with_the_runtime_default_seccomp_profile(self) -> None:
        for label, _, document in self.every_workload():
            with self.subTest(workload=label):
                context = pod_spec(document).get("securityContext") or {}
                self.assertIs(context.get("runAsNonRoot"), True)
                self.assertGreater(context.get("runAsUser", 0), 0)
                self.assertEqual(context.get("seccompProfile"), {"type": "RuntimeDefault"})

    def test_pods_never_share_host_namespaces(self) -> None:
        for label, _, document in self.every_workload():
            with self.subTest(workload=label):
                spec = pod_spec(document)
                for key in ("hostNetwork", "hostPID", "hostIPC"):
                    self.assertFalse(spec.get(key), key)

    def test_every_container_is_unprivileged_with_no_capabilities(self) -> None:
        for label, name, document in self.every_workload():
            spec = pod_spec(document)
            containers = list(spec.get("initContainers", [])) + list(spec["containers"])
            self.assertTrue(containers, label)
            for container in containers:
                with self.subTest(workload=label, container=container["name"]):
                    context = container.get("securityContext") or {}
                    self.assertIs(context.get("allowPrivilegeEscalation"), False)
                    self.assertFalse(context.get("privileged"))
                    self.assertEqual((context.get("capabilities") or {}).get("drop"), ["ALL"])
                    self.assertFalse((context.get("capabilities") or {}).get("add"))
                    if name in WRITABLE_ROOT:
                        self.assertIsNot(
                            context.get("readOnlyRootFilesystem"), True,
                            f"{name} now has a read-only root; drop it from WRITABLE_ROOT",
                        )
                    else:
                        self.assertIs(context.get("readOnlyRootFilesystem"), True)

    def test_only_the_named_workloads_mount_a_service_account_token(self) -> None:
        for label, name, document in self.every_workload():
            with self.subTest(workload=label):
                mounted = pod_spec(document).get("automountServiceAccountToken")
                if name in MOUNTS_SERVICE_ACCOUNT_TOKEN:
                    self.assertIsNot(
                        mounted, False,
                        f"{name} no longer mounts a token; drop it from MOUNTS_SERVICE_ACCOUNT_TOKEN",
                    )
                else:
                    self.assertIs(mounted, False)


if __name__ == "__main__":
    unittest.main()
