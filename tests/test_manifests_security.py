"""Security invariants of the generated Runtime Pod (control_plane/manifests.py).

``runtime_pod_manifest`` is a pure function, so every hardening field the
cluster scripts check after deployment (``scripts/test.sh``) is pinned here
where CI actually runs it: sandbox runtime class, no service-account token,
read-only root filesystems, non-root uid, dropped capabilities, seccomp,
node placement derived from the selector, and the tenant label.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_plane.manifests import (  # noqa: E402
    ManifestSettings,
    runtime_pod_manifest,
    runtime_service_manifest,
)

SANDBOX_ID = "sb-0123456789ab"
WORKSPACE_ID = "ws-aaaaaaaaaaaa"
TENANT = "acme"
SELECTOR = {"convee.io/runtime-node": "true"}
TOLERATIONS = ({
    "key": "convee.io/runtime-node",
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
},)
NON_ROOT_UID = 65532


def settings(
    *,
    runtime_class: str = "gvisor",
    node_selector: dict[str, str] | None = SELECTOR,
    storage_mode: str = "per-workspace",
) -> ManifestSettings:
    return ManifestSettings(
        workload_namespace="sandbox-workloads",
        workspace_pvc="workspaces",
        workspace_storage_mode=storage_mode,
        runtime_class=runtime_class,
        runtime_node_selector=dict(node_selector or {}),
        runtime_tolerations=TOLERATIONS if node_selector else (),
        runtime_ttl_seconds=1800,
        runtime_hard_ttl_seconds=7200,
        runtime_name=lambda sandbox_id: f"sandbox-{sandbox_id}",
        template_image=lambda template_id, tenant_id: f"registry.example/{template_id}:1",
        capability_key=lambda kind, subject: f"{kind}-key-{subject}",
        capability_epoch=lambda kind, subject: 7,
    )


def all_containers(manifest: dict) -> list[dict]:
    spec = manifest["spec"]
    containers = list(spec.get("initContainers", [])) + list(spec["containers"])
    assert containers, "expected at least one runtime container"
    return containers


class RuntimePodSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = runtime_pod_manifest(
            settings(), SANDBOX_ID, WORKSPACE_ID, "default", TENANT
        )
        self.spec = self.manifest["spec"]

    def test_pod_runs_under_the_configured_runtime_class(self) -> None:
        self.assertEqual(self.spec["runtimeClassName"], "gvisor")

    def test_runtime_class_key_is_absent_only_when_unconfigured(self) -> None:
        plain = runtime_pod_manifest(
            settings(runtime_class=""), SANDBOX_ID, WORKSPACE_ID
        )
        self.assertNotIn("runtimeClassName", plain["spec"])

    def test_service_account_token_is_never_mounted(self) -> None:
        self.assertIn("automountServiceAccountToken", self.spec)
        self.assertIs(self.spec["automountServiceAccountToken"], False)
        self.assertIs(self.spec["enableServiceLinks"], False)
        self.assertNotIn("serviceAccountName", self.spec)

    def test_every_container_has_a_read_only_root_filesystem(self) -> None:
        for container in all_containers(self.manifest):
            with self.subTest(container=container["name"]):
                context = container["securityContext"]
                self.assertIn("readOnlyRootFilesystem", context)
                self.assertIs(context["readOnlyRootFilesystem"], True)

    def test_every_container_drops_all_capabilities(self) -> None:
        for container in all_containers(self.manifest):
            with self.subTest(container=container["name"]):
                context = container["securityContext"]
                self.assertEqual(context["capabilities"], {"drop": ["ALL"]})
                self.assertIs(context["allowPrivilegeEscalation"], False)
                self.assertFalse(context.get("privileged", False))

    def test_pod_runs_as_the_non_root_sandbox_user(self) -> None:
        context = self.spec["securityContext"]
        self.assertIs(context["runAsNonRoot"], True)
        self.assertEqual(context["runAsUser"], NON_ROOT_UID)
        self.assertEqual(context["runAsGroup"], NON_ROOT_UID)
        self.assertEqual(context["fsGroup"], NON_ROOT_UID)
        for container in all_containers(self.manifest):
            self.assertNotEqual(
                container["securityContext"].get("runAsUser"), 0
            )

    def test_pod_uses_the_runtime_default_seccomp_profile(self) -> None:
        self.assertEqual(
            self.spec["securityContext"]["seccompProfile"],
            {"type": "RuntimeDefault"},
        )

    def test_pod_never_shares_host_namespaces(self) -> None:
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertFalse(self.spec.get(key, False), key)
        volume_kinds = {
            kind for volume in self.spec["volumes"] for kind in volume if kind != "name"
        }
        self.assertEqual(volume_kinds, {"persistentVolumeClaim", "emptyDir"})

    def test_node_selector_and_toleration_are_explicit_independent_settings(self) -> None:
        self.assertEqual(self.spec["nodeSelector"], SELECTOR)
        self.assertEqual(
            self.spec["tolerations"],
            [
                {
                    "key": "convee.io/runtime-node",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
        )
        unpinned = runtime_pod_manifest(
            settings(node_selector=None), SANDBOX_ID, WORKSPACE_ID
        )
        self.assertNotIn("nodeSelector", unpinned["spec"])
        self.assertNotIn("tolerations", unpinned["spec"])

    def test_restart_policy_is_pinned(self) -> None:
        self.assertEqual(self.spec["restartPolicy"], "Always")

    def test_runtime_has_no_redundant_init_container(self) -> None:
        self.assertNotIn("initContainers", self.spec)

    def test_pod_carries_identity_labels_and_tenant(self) -> None:
        labels = self.manifest["metadata"]["labels"]
        self.assertEqual(labels["convee.io/tenant"], TENANT)
        self.assertEqual(labels["convee.io/sandbox-id"], SANDBOX_ID)
        self.assertEqual(labels["convee.io/workspace-id"], WORKSPACE_ID)
        self.assertEqual(labels["convee.io/template"], "default")
        self.assertEqual(labels["app.kubernetes.io/name"], "sandbox-runtime")
        self.assertEqual(self.manifest["metadata"]["name"], f"sandbox-{SANDBOX_ID}")
        self.assertEqual(self.manifest["metadata"]["namespace"], "sandbox-workloads")

    def test_single_tenant_pod_has_no_tenant_label(self) -> None:
        manifest = runtime_pod_manifest(settings(), SANDBOX_ID, WORKSPACE_ID)
        self.assertNotIn("convee.io/tenant", manifest["metadata"]["labels"])

    def test_expiry_annotations_follow_the_ttl_settings(self) -> None:
        annotations = self.manifest["metadata"]["annotations"]
        created = int(annotations["convee.io/created-at"])
        self.assertEqual(int(annotations["convee.io/expires-at"]), created + 1800)
        self.assertEqual(int(annotations["convee.io/hard-expires-at"]), created + 7200)

    def test_images_come_from_the_template_registry_only(self) -> None:
        for container in all_containers(self.manifest):
            self.assertEqual(container["image"], "registry.example/default:1")
            self.assertEqual(container["imagePullPolicy"], "IfNotPresent")

    def test_runtime_container_gets_scoped_tokens_and_bounded_resources(self) -> None:
        container = self.spec["containers"][0]
        env = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(env["SANDBOX_CAPABILITY_KEY"], f"runtime-key-{SANDBOX_ID}")
        self.assertEqual(env["SANDBOX_CAPABILITY_EPOCH"], "7")
        self.assertEqual(
            env["WORKSPACE_CAPABILITY_KEY"], f"workspace-key-{WORKSPACE_ID}"
        )
        self.assertEqual(env["WORKSPACE_CAPABILITY_EPOCH"], "7")
        self.assertEqual(env["SANDBOX_ID"], SANDBOX_ID)
        self.assertEqual(env["WORKSPACE_ID"], WORKSPACE_ID)
        for container in all_containers(self.manifest):
            limits = container["resources"]["limits"]
            self.assertIn("cpu", limits)
            self.assertIn("memory", limits)
        self.assertEqual(self.spec["containers"][0]["resources"]["limits"]["memory"], "512Mi")

    def test_health_probes_are_http_not_exec(self) -> None:
        container = self.spec["containers"][0]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertIn("httpGet", container[probe], probe)
            self.assertNotIn("exec", container[probe], probe)
            self.assertEqual(container[probe]["httpGet"]["path"], "/healthz")

    def test_startup_and_readiness_probes_are_prompt_without_shortening_windows(self) -> None:
        container = self.spec["containers"][0]
        startup = container["startupProbe"]
        readiness = container["readinessProbe"]
        self.assertEqual(startup["periodSeconds"], 1)
        self.assertGreaterEqual(
            startup["periodSeconds"] * startup["failureThreshold"], 60
        )
        self.assertEqual(readiness["periodSeconds"], 1)
        self.assertGreaterEqual(
            readiness["periodSeconds"] * readiness["failureThreshold"], 30
        )

    def test_workspace_volume_follows_the_storage_mode(self) -> None:
        claim = self.spec["volumes"][0]["persistentVolumeClaim"]["claimName"]
        self.assertEqual(claim, WORKSPACE_ID)
        mount = self.spec["containers"][0]["volumeMounts"][0]
        self.assertEqual(mount["mountPath"], "/workspace")
        self.assertNotIn("subPath", mount)

        shared = runtime_pod_manifest(
            settings(storage_mode="shared"), SANDBOX_ID, WORKSPACE_ID
        )
        claim = shared["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        self.assertEqual(claim, "workspaces")
        mount = shared["spec"]["containers"][0]["volumeMounts"][0]
        self.assertEqual(mount["subPath"], WORKSPACE_ID)


class RuntimeServiceTests(unittest.TestCase):
    def test_service_selects_exactly_its_own_pod(self) -> None:
        service = runtime_service_manifest(settings(), SANDBOX_ID)
        pod = runtime_pod_manifest(settings(), SANDBOX_ID, WORKSPACE_ID, "default", TENANT)
        labels = pod["metadata"]["labels"]
        for key, value in service["spec"]["selector"].items():
            self.assertEqual(labels[key], value, key)
        self.assertEqual(service["metadata"]["name"], pod["metadata"]["name"])
        self.assertEqual(service["spec"]["ports"], [
            {"name": "mcp", "port": 8080, "targetPort": "mcp"},
        ])


if __name__ == "__main__":
    unittest.main()
