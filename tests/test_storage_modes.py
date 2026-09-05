from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import unittest

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


sys.path.insert(0, str(REPO_ROOT))
from control_plane import manifests  # noqa: E402


def manifest_settings(storage_mode: str) -> manifests.ManifestSettings:
    return manifests.ManifestSettings(
        workload_namespace="sandbox-workloads",
        workspace_pvc="sandbox-workspaces",
        workspace_storage_mode=storage_mode,
        runtime_class="gvisor",
        runtime_node_selector={},
        runtime_tolerations=(),
        runtime_ttl_seconds=3600,
        runtime_hard_ttl_seconds=7200,
        runtime_name=lambda sandbox_id: f"runtime-{sandbox_id}",
        template_image=lambda _template, _tenant: "sandbox-runtime:test",
        capability_key=lambda kind, resource_id: f"{kind}-{resource_id}",
        capability_epoch=lambda kind, resource_id: 1,
    )


class WorkspaceMountTests(unittest.TestCase):
    def test_per_workspace_mode_mounts_the_dedicated_claim_root(self) -> None:
        self.assertEqual(
            manifests._workspace_mount(
                manifest_settings("per-workspace"), "ws-123"
            ),
            {"name": "workspaces", "mountPath": "/workspace"},
        )

    def test_shared_mode_isolates_the_workspace_with_subpath(self) -> None:
        self.assertEqual(
            manifests._workspace_mount(manifest_settings("shared"), "ws-123"),
            {
                "name": "workspaces",
                "mountPath": "/workspace",
                "subPath": "ws-123",
            },
        )


class LocalDevelopmentManifestTests(unittest.TestCase):
    def test_rook_v1_20_installs_the_ceph_connection_crd_provider(self) -> None:
        text = (REPO_ROOT / "scripts/local-cluster.sh").read_text()
        self.assertIn("--set csi.installCsiOperator=true", text)
        self.assertNotIn("--set csi.installCsiOperator=false", text)
        self.assertIn("helm pull ceph-csi-drivers", text)
        self.assertIn("CEPH_CSI_DRIVERS_CHART_SHA256", text)
        self.assertIn("v3.17.1@sha256:", text)
        self.assertIn("rook/ceph-csi-drivers-values.yaml", text)

        values = yaml.safe_load(
            (REPO_ROOT / "rook/ceph-csi-drivers-values.yaml").read_text()
        )
        self.assertTrue(values["drivers"]["cephfs"]["enabled"])
        self.assertEqual(
            values["drivers"]["cephfs"]["name"],
            "rook-ceph.cephfs.csi.ceph.com",
        )
        self.assertEqual(
            values["drivers"]["cephfs"]["cephFsClientType"], "autodetect"
        )
        self.assertEqual(
            values["operatorConfig"]["driverSpecDefaults"]["nodePlugin"][
                "tolerations"
            ][0]["key"],
            "sandbox.hullwork.com/node-role",
        )
        for driver in ("rbd", "cephfs"):
            self.assertEqual(
                values["drivers"][driver]["nodePlugin"]["tolerations"][0][
                    "key"
                ],
                "sandbox.hullwork.com/node-role",
            )

    def test_local_rook_selects_its_loop_device_by_exact_path(self) -> None:
        cluster = next(
            document for document in yaml.safe_load_all(
                (REPO_ROOT / "rook/cluster-local.yaml").read_text()
            )
            if document and document.get("kind") == "CephCluster"
        )
        storage = cluster["spec"]["storage"]
        self.assertFalse(storage["useAllNodes"])
        self.assertEqual(
            storage["nodes"],
            [{"name": "__SANDBOX_CONTROL_PLANE_NODE__", "devices": [{"name": "/dev/loop0"}]}],
        )
        self.assertNotIn("deviceFilter", storage)

    def test_local_workspace_storage_is_cephfs_rwx(self) -> None:
        documents = [
            item for item in yaml.safe_load_all(
                (REPO_ROOT / "rook/cluster-local.yaml").read_text()
            )
            if item
        ]
        filesystem = next(item for item in documents if item["kind"] == "CephFilesystem")
        storage_class = next(item for item in documents if item["kind"] == "StorageClass")
        self.assertEqual(filesystem["metadata"]["name"], "sandbox-filesystem")
        self.assertEqual(storage_class["metadata"]["name"], "sandbox-rwx")
        self.assertEqual(
            storage_class["provisioner"], "rook-ceph.cephfs.csi.ceph.com"
        )
        self.assertEqual(storage_class["parameters"]["fsName"], "sandbox-filesystem")
        self.assertEqual(
            storage_class["parameters"]["pool"],
            "sandbox-filesystem-replicated",
        )
        self.assertEqual(storage_class["parameters"]["mounter"], "fuse")
        self.assertEqual(
            storage_class["parameters"][
                "csi.storage.k8s.io/controller-publish-secret-name"
            ],
            "rook-csi-cephfs-provisioner",
        )
        local_overlay = (REPO_ROOT / "overlays/local/kustomization.yaml").read_text()
        self.assertIn("resources:\n  - ../../k8s", local_overlay)
        self.assertNotIn("rwo-single-node", local_overlay)

    def test_rook_object_store_waits_for_the_reported_phase(self) -> None:
        text = (REPO_ROOT / "scripts/local-cluster.sh").read_text()
        self.assertIn('"--for=jsonpath={.status.phase}=Ready" cephobjectstore/object-store', text)
        self.assertNotIn("--for=condition=Ready cephobjectstore", text)

    def test_storage_class_upgrade_is_data_preserving(self) -> None:
        text = (REPO_ROOT / "scripts/local-cluster.sh").read_text()
        reconcile = text[
            text.index("reconcile_workspace_storage_class()") :
            text.index("deploy_ceph_rgw()")
        ]
        self.assertIn("get pv", reconcile)
        self.assertIn("get pvc --all-namespaces", reconcile)
        self.assertIn("will not replace it while PVs or PVCs exist", reconcile)
        self.assertLess(
            reconcile.index('if [ -n "$referenced_pvs" ]'),
            reconcile.index("delete storageclass sandbox-rwx"),
        )
        self.assertIn("reconcile_workspace_storage_class", text)

    def test_lima_restarts_fixed_tag_project_deployments_only_on_update(self) -> None:
        text = (REPO_ROOT / "scripts/local-cluster.sh").read_text()
        detection_at = text.index("system_deployments_to_restart=()")
        apply_at = text.index("apply_local_overlay", detection_at)
        system_restart_at = text.index('rollout restart "${system_deployments_to_restart[@]}"')
        volume_restart_at = text.index(
            "rollout restart deployment/sandbox-volume"
        )
        status_at = text.index("deployment/sandbox-control-plane --timeout=3m")
        self.assertLess(detection_at, apply_at)
        self.assertLess(apply_at, system_restart_at)
        self.assertLess(system_restart_at, status_at)
        self.assertLess(volume_restart_at, status_at)
        self.assertIn("deployment_runs_host_image", text)
        self.assertIn('if ((${#system_deployments_to_restart[@]}))', text)
        self.assertIn("if ((restart_volume_deployment))", text)

    def test_lima_waits_only_for_objects_the_local_overlay_renders(self) -> None:
        if shutil.which("kubectl") is None:
            self.skipTest("kubectl is required to render overlays/local")
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(REPO_ROOT / "overlays/local")],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        present = {
            (document["kind"].lower(), document["metadata"].get("namespace", ""), document["metadata"]["name"])
            for document in yaml.safe_load_all(rendered)
            if document
        }
        script = (REPO_ROOT / "scripts/local-cluster.sh").read_text()
        # Every object the bootstrap script blocks on must come from the
        # profile it just applied; a stale name here hangs `make up-local`.
        awaited = [
            (kind, namespace, name)
            for namespace, kind, name in re.findall(
                r"-n (\S+) (?:rollout status|wait) \\\n\s+(?:--for=\S+ )?(deployment|job)/(\S+)",
                script,
            )
        ]
        self.assertGreaterEqual(len(awaited), 6)
        self.assertEqual([entry for entry in awaited if entry not in present], [])
        for expected in (
            ("job", "sandbox-system", "sandbox-object-store-init"),
            ("deployment", "kube-system", "metrics-server"),
        ):
            self.assertIn(expected, awaited)

    def test_tuning_configmap_is_generated_in_control_plane_namespace(self) -> None:
        manifest = yaml.safe_load((REPO_ROOT / "k8s/kustomization.yaml").read_text())
        tuning = next(
            item for item in manifest["configMapGenerator"]
            if item["name"] == "sandbox-tuning"
        )
        self.assertEqual(tuning["namespace"], "sandbox-system")

    def test_sqlite_patch_keeps_the_tmp_mount(self) -> None:
        patch = yaml.safe_load(
            (REPO_ROOT / "overlays/local-dev/control-plane-sqlite.yaml").read_text()
        )
        volume_mount = patch["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ][0]
        self.assertEqual(volume_mount["$patch"], "delete")
        self.assertEqual(volume_mount["mountPath"], "/var/run/sandbox-db")

    def test_rwo_recreate_strategy_removes_rolling_update(self) -> None:
        documents = list(yaml.safe_load_all(
            (REPO_ROOT / "overlays/rwo-single-node/volume-agent-single.yaml").read_text()
        ))
        strategy = documents[0]["spec"]["strategy"]
        self.assertEqual(strategy, {"type": "Recreate", "rollingUpdate": None})

    def test_local_object_store_uses_rook_generated_application_credentials(self) -> None:
        text = (REPO_ROOT / "overlays/local-dev/object-store.yaml").read_text()
        rook = (REPO_ROOT / "rook/cluster-local.yaml").read_text()
        self.assertNotIn("object-store-root-credentials", text)
        # No administrative API, whatever client is used to reach it.
        self.assertNotIn("mc admin", text)
        self.assertNotIn("put_bucket_policy", text)
        self.assertIn("kind: CephObjectStoreUser", rook)
        self.assertIn("name: sandbox-runtime", rook)
        self.assertIn("maxBuckets: 3", rook)
        # Versioning on every bucket the Job creates. This counted
        # `mc version enable` until the MinIO Client was removed; counting the
        # command was always a proxy for "all three, none forgotten", so the
        # proxy moved and the assertion did not.
        self.assertEqual(text.count("OBJECT_STORE_UPLOAD_BUCKET"), 2)
        self.assertEqual(text.count("OBJECT_STORE_AGENT_BUCKET"), 2)
        self.assertEqual(text.count("OBJECT_STORE_WORKSPACE_BUCKET"), 2)
        self.assertIn("VersioningConfiguration", text)
        self.assertIn("existing_buckets", text)
        self.assertIn("if bucket not in existing_buckets", text)

if __name__ == "__main__":
    unittest.main()
