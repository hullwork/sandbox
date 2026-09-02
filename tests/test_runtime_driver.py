"""Contract tests for the first Control Plane Runtime Driver."""

from __future__ import annotations

import pathlib
import sys
import unittest
from http import HTTPStatus


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_plane import (  # noqa: E402
    RuntimeDriverError,
    RuntimeDriverErrorCode,
    RuntimeInstance,
    RuntimeSpec,
)
from control_plane.drivers import GVisorRuntimeDriver  # noqa: E402
from control_plane.kube import KubeError  # noqa: E402
from control_plane.manifests import ManifestSettings  # noqa: E402


class FakeKube:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list(self, namespace, resource, *, label_selector=""):
        self.calls.append(("list", namespace, resource, label_selector))
        return []

    def get(self, namespace, resource, name):
        self.calls.append(("get", namespace, resource, name))
        return {"metadata": {"name": name}}

    def list_group(
        self, namespace, group, version, resource, *, label_selector=""
    ):
        self.calls.append(
            ("list_group", namespace, group, version, resource, label_selector)
        )
        return []

    def create_or_get(self, namespace, resource, name, manifest):
        self.calls.append(("create_or_get", namespace, resource, name, manifest))
        return manifest

    def patch_annotations(self, namespace, resource, name, annotations):
        self.calls.append(("patch_annotations", namespace, resource, name, annotations))
        return {"metadata": {"name": name, "annotations": annotations}}

    def delete(self, namespace, resource, name):
        self.calls.append(("delete", namespace, resource, name))


def settings() -> ManifestSettings:
    return ManifestSettings(
        workload_namespace="sandbox-workloads",
        workspace_pvc="sandbox-workspaces",
        workspace_storage_mode="shared",
        runtime_class="gvisor",
        runtime_node_selector={"sandbox.convee.io/node-role": "runtime"},
        runtime_tolerations=({
            "key": "sandbox.convee.io/node-role",
            "operator": "Equal",
            "value": "runtime",
            "effect": "NoSchedule",
        },),
        runtime_ttl_seconds=1800,
        runtime_hard_ttl_seconds=43200,
        runtime_name=lambda runtime_id: f"runtime-{runtime_id}",
        template_image=lambda template_id, tenant_id: f"images/{template_id}",
        capability_key=lambda kind, subject: f"key-{kind}-{subject}",
        capability_epoch=lambda kind, subject: 1,
    )


class RuntimeDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kube = FakeKube()
        self.driver = GVisorRuntimeDriver(self.kube, settings())

    def test_capabilities_name_only_features_implemented_today(self) -> None:
        capabilities = self.driver.capabilities
        self.assertEqual(capabilities.driver, "gvisor")
        self.assertEqual(capabilities.isolation, "sandboxed-kernel")
        self.assertEqual(capabilities.isolation_profile, "gvisor")
        self.assertNotIn("suspend", capabilities.__dataclass_fields__)
        self.assertNotIn("resume", capabilities.__dataclass_fields__)

    def test_empty_runtime_class_reports_cluster_default_not_gvisor_isolation(self) -> None:
        plain_settings = settings()
        plain_settings = ManifestSettings(
            **{
                **plain_settings.__dict__,
                "runtime_class": "",
            }
        )
        capabilities = GVisorRuntimeDriver(self.kube, plain_settings).capabilities
        self.assertEqual(capabilities.isolation, "cluster-default")

    def test_create_owns_provider_specific_manifest_generation(self) -> None:
        runtime = self.driver.create_runtime(
            RuntimeSpec("sb-0123456789ab", "ws-aaaaaaaaaaaa", "default", "acme")
        )
        self.assertIsInstance(runtime, RuntimeInstance)
        manifest = self.kube.calls[-1][4]
        self.assertEqual(manifest["spec"]["runtimeClassName"], "gvisor")
        self.assertEqual(
            manifest["metadata"]["labels"]["convee.io/workspace-id"],
            "ws-aaaaaaaaaaaa",
        )

    def test_workspace_lookup_and_endpoint_are_driver_owned(self) -> None:
        self.driver.list_for_workspace("ws-aaaaaaaaaaaa")
        self.assertEqual(
            self.kube.calls[-1],
            (
                "list",
                "sandbox-workloads",
                "pods",
                "app.kubernetes.io/name=sandbox-runtime,"
                "convee.io/workspace-id=ws-aaaaaaaaaaaa",
            ),
        )
        self.assertEqual(
            self.driver.endpoint("sb-0123456789ab"),
            "http://runtime-sb-0123456789ab.sandbox-workloads.svc.cluster.local:8080",
        )

    def test_runtime_metrics_are_provider_owned(self) -> None:
        self.driver.list_runtime_metrics()
        self.assertEqual(
            self.kube.calls[-1],
            (
                "list_group",
                "sandbox-workloads",
                "metrics.k8s.io",
                "v1beta1",
                "pods",
                "app.kubernetes.io/name=sandbox-runtime",
            ),
        )

    def test_pod_shape_is_normalized_before_it_leaves_the_driver(self) -> None:
        runtime = self.driver._instance({
            "metadata": {
                "name": "runtime-sb-0123456789ab",
                "labels": {
                    "convee.io/sandbox-id": "sb-0123456789ab",
                    "convee.io/workspace-id": "ws-aaaaaaaaaaaa",
                },
                "annotations": {"convee.io/expires-at": "1700001800"},
            },
            "spec": {
                "runtimeClassName": "gvisor",
                "nodeName": "node-a",
                "containers": [{"resources": {
                    "requests": {"cpu": "250m", "memory": "256Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                }}],
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"restartCount": 2}],
            },
        })
        self.assertEqual(runtime.runtime_id, "sb-0123456789ab")
        self.assertTrue(runtime.ready)
        self.assertEqual(runtime.cpu_request_millicores, 250)
        self.assertEqual(runtime.memory_limit_bytes, 1024**3)

    def test_kubernetes_errors_are_translated_at_the_driver_boundary(self) -> None:
        class BrokenKube(FakeKube):
            def get(self, namespace, resource, name):
                raise KubeError(HTTPStatus.SERVICE_UNAVAILABLE, "apiserver busy")

        driver = GVisorRuntimeDriver(BrokenKube(), settings())
        with self.assertRaises(RuntimeDriverError) as raised:
            driver.get_runtime("sb-0123456789ab")
        self.assertEqual(raised.exception.code, RuntimeDriverErrorCode.UNAVAILABLE)
        self.assertEqual(raised.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_delete_preserves_service_then_pod_order(self) -> None:
        self.driver.delete_runtime("sb-0123456789ab")
        self.assertEqual(
            self.kube.calls,
            [
                ("delete", "sandbox-workloads", "services", "runtime-sb-0123456789ab"),
                ("delete", "sandbox-workloads", "pods", "runtime-sb-0123456789ab"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
