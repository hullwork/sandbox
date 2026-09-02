from __future__ import annotations

import ast
import pathlib
import sys
import types
import unittest
from decimal import Decimal, InvalidOperation
from http import HTTPStatus

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_plane.kube import KubeClient, KubeError  # noqa: E402
from control_plane import (  # noqa: E402
    RuntimeDriverError,
    RuntimeDriverErrorCode,
    RuntimeInstance,
    RuntimeUsage,
)


def load_monitoring_helpers() -> dict:
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "cpu_millicores",
        "memory_bytes",
        "runtime_monitoring_view",
        "node_monitoring_view",
    }
    constants = {"_BINARY_QUANTITY_UNITS", "_DECIMAL_QUANTITY_UNITS"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef) and node.name in wanted
        ) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
        )
    ]
    module = ast.Module(body=body, type_ignores=[])
    namespace = {
        "Decimal": Decimal,
        "InvalidOperation": InvalidOperation,
        "RuntimeInstance": RuntimeInstance,
        "RuntimeUsage": RuntimeUsage,
        "sandbox_view": lambda runtime: {
            "id": runtime.runtime_id,
            "workspace_id": runtime.workspace_id,
            "status": runtime.state,
        },
    }
    exec(compile(ast.fix_missing_locations(module), "core.py", "exec"), namespace)
    return namespace


HELPERS = load_monitoring_helpers()


def load_monitoring_handler(control_plane_module: object):
    source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    original = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApiHandler"
    )
    methods = [
        node for node in original.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_metrics_failure", "monitoring_view"}
    ]
    harness = ast.ClassDef(
        name="MonitoringHandler",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    namespace = {
        "HTTPStatus": HTTPStatus,
        "KubeError": KubeError,
        "control_plane": control_plane_module,
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])), "api.py", "exec"),
        namespace,
    )
    return namespace["MonitoringHandler"]


class MonitoringContractTests(unittest.TestCase):
    def test_kubernetes_quantity_normalization(self) -> None:
        cpu = HELPERS["cpu_millicores"]
        memory = HELPERS["memory_bytes"]
        self.assertEqual(cpu("250m"), 250)
        self.assertEqual(cpu("2"), 2000)
        self.assertEqual(cpu("125000000n"), 125)
        self.assertEqual(memory("512Mi"), 512 * 1024**2)
        self.assertEqual(memory("2G"), 2_000_000_000)
        self.assertIsNone(cpu("broken"))
        self.assertIsNone(memory(None))

    def test_runtime_view_keeps_declared_resources_without_metrics(self) -> None:
        runtime = RuntimeInstance(
            runtime_id="sb-0123456789ab",
            workspace_id="ws-0123456789ab",
            provider_id="runtime-sb-0123456789ab",
            state="running",
            ready=True,
            isolation="gvisor",
            node="node-a",
            restarts=2,
            cpu_request_millicores=250,
            cpu_limit_millicores=1000,
            memory_request_bytes=256 * 1024**2,
            memory_limit_bytes=1024**3,
        )
        view = HELPERS["runtime_monitoring_view"](runtime)
        self.assertEqual(view["node"], "node-a")
        self.assertEqual(view["restarts"], 2)
        self.assertEqual(view["cpu"]["request_millicores"], 250)
        self.assertEqual(view["memory"]["limit_bytes"], 1024**3)
        self.assertIsNone(view["cpu"]["usage_millicores"])

    def test_node_view_combines_core_and_metrics_api(self) -> None:
        node = {
            "metadata": {
                "name": "node-a",
                "labels": {"node-role.kubernetes.io/control-plane": ""},
            },
            "spec": {},
            "status": {
                "capacity": {"cpu": "2", "memory": "4Gi", "pods": "110"},
                "allocatable": {"cpu": "1800m", "memory": "3Gi"},
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeInfo": {"kubeletVersion": "v1.36.4", "architecture": "arm64"},
            },
        }
        metric = {"usage": {"cpu": "125000000n", "memory": "512Mi"}}
        view = HELPERS["node_monitoring_view"](node, metric)
        self.assertEqual(view["status"], "ready")
        self.assertEqual(view["roles"], ["control-plane"])
        self.assertEqual(view["cpu"]["usage_millicores"], 125)
        self.assertEqual(view["cpu"]["allocatable_millicores"], 1800)
        self.assertEqual(view["memory"]["usage_bytes"], 512 * 1024**2)

    def test_kube_client_has_cluster_scoped_monitoring_paths(self) -> None:
        self.assertEqual(KubeClient.cluster_path("nodes"), "/api/v1/nodes")
        self.assertEqual(
            KubeClient.group_cluster_path("metrics.k8s.io", "v1beta1", "nodes"),
            "/apis/metrics.k8s.io/v1beta1/nodes",
        )

    def test_monitoring_rbac_is_read_only(self) -> None:
        documents = list(yaml.safe_load_all((ROOT / "k8s/rbac.yaml").read_text()))
        role = next(
            item for item in documents
            if item.get("kind") == "ClusterRole"
            and item.get("metadata", {}).get("name") == "sandbox-monitoring"
        )
        self.assertEqual(
            {(tuple(rule["apiGroups"]), tuple(rule["resources"]), tuple(rule["verbs"])) for rule in role["rules"]},
            {
                (("",), ("nodes",), ("get", "list")),
                (("metrics.k8s.io",), ("nodes",), ("get", "list")),
            },
        )
        workload_role = next(
            item for item in documents
            if item.get("kind") == "Role"
            and item.get("metadata", {}).get("name") == "sandbox-lifecycle"
        )
        pod_metrics = next(
            rule for rule in workload_role["rules"]
            if rule["apiGroups"] == ["metrics.k8s.io"]
        )
        self.assertEqual(pod_metrics, {
            "apiGroups": ["metrics.k8s.io"],
            "resources": ["pods"],
            "verbs": ["get", "list"],
        })

    def test_tenant_monitoring_does_not_query_or_disclose_nodes(self) -> None:
        class FakeKube:
            def list_runtime_metrics(self):
                raise RuntimeDriverError(
                    RuntimeDriverErrorCode.NOT_FOUND,
                    "metrics API missing",
                    status=HTTPStatus.NOT_FOUND,
                )

            def list_cluster(self, plural):
                raise AssertionError("tenant monitoring must not list nodes")

        fake_kube = FakeKube()
        control_plane_module = types.SimpleNamespace(
            KUBE=fake_kube,
            configured_runtime_driver=lambda: fake_kube,
            WORKLOAD_NAMESPACE="sandbox-workloads",
            RuntimeDriverError=RuntimeDriverError,
            RuntimeInstance=RuntimeInstance,
            RuntimeUsage=RuntimeUsage,
            runtime_monitoring_view=lambda runtime, metric: {
                "id": runtime.runtime_id, "node": "shared-node"
            },
            node_monitoring_view=lambda node, metric: node,
        )
        handler = load_monitoring_handler(control_plane_module)()
        handler.tenant_id = "acme"
        handler.scope_sandboxes = lambda runtimes: [{"id": "sb-visible"}]
        result = handler.monitoring_view([
            RuntimeInstance("sb-visible", "ws-visible", "visible", "running", True, "gvisor"),
            RuntimeInstance("sb-hidden", "ws-hidden", "hidden", "running", True, "gvisor"),
        ])
        self.assertEqual(result["scope"], "tenant")
        self.assertFalse(result["nodes_visible"])
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["runtimes"], [{"id": "sb-visible", "node": None}])
        self.assertEqual(
            result["metrics"]["runtimes"],
            {"available": False, "reason": "metrics_api_unavailable"},
        )


class LocalMetricsServerTests(unittest.TestCase):
    IMAGE = (
        "registry.k8s.io/metrics-server/metrics-server@"
        "sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0"
    )

    def test_local_manifest_is_pinned_and_explicitly_local_only(self) -> None:
        path = ROOT / "overlays/local-dev/metrics-server.yaml"
        source = path.read_text(encoding="utf-8")
        documents = list(yaml.safe_load_all(source))
        deployment = next(item for item in documents if item.get("kind") == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]

        self.assertEqual(container["image"], self.IMAGE)
        self.assertIn("--kubelet-insecure-tls", container["args"])
        self.assertIn("v0.9.0/components.yaml", source)
        self.assertIn(
            "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b",
            source,
        )
        self.assertIn("metrics-server.yaml", (ROOT / "overlays/local-dev/kustomization.yaml").read_text())

    def test_metrics_server_is_absent_from_production_render(self) -> None:
        base_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "k8s").glob("*.yaml")
        )
        self.assertNotIn("--kubelet-insecure-tls", base_sources)
        self.assertNotIn("name: metrics-server", base_sources)

    def test_local_installers_use_same_digest_and_readiness_gates(self) -> None:
        installer = (ROOT / "scripts/local-cluster.sh").read_text(encoding="utf-8")
        self.assertIn(self.IMAGE, installer)
        self.assertIn("deployment/metrics-server", installer)
        self.assertIn("apiservice/v1beta1.metrics.k8s.io", installer)


if __name__ == "__main__":
    unittest.main()
