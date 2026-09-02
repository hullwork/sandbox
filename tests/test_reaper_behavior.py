"""Behavioral invariants of the runtime reaper (control_plane/reaper.py).

``reaper.py`` imports the package's ``core`` module, which reads its environment
on import, so the tests install a minimal fake core before loading the reaper.
The fake records every Kubernetes call by path and every store call
so each test can assert exactly what one sweep did.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import pathlib
import sys
import types
import unittest
from http import HTTPStatus

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_plane import (  # noqa: E402
    RuntimeDriverError,
    RuntimeDriverErrorCode,
    RuntimeInstance,
)
from control_plane.kube import KubeError  # noqa: E402
from control_plane.store import StoreError  # noqa: E402

NAMESPACE = "sandbox-workloads"
RUNTIME_SELECTOR = "app.kubernetes.io/name=sandbox-runtime"
NOW = 1_700_000_000


class FakeKube:
    """Kubernetes client double that answers per (namespace, plural)."""

    def __init__(self, pods: list[dict]) -> None:
        self.pods = pods
        self.calls: list[tuple] = []
        self.list_calls = 0

    def list(self, namespace: str, plural: str, label_selector: str | None = None):
        self.calls.append(("list", namespace, plural, label_selector))
        if namespace == NAMESPACE and plural == "pods":
            self.list_calls += 1
            return list(self.pods)
        raise AssertionError(f"unexpected list {namespace}/{plural}")

    def delete(self, namespace: str, plural: str, name: str) -> None:
        self.calls.append(("delete", namespace, plural, name))
        if plural not in {"pods", "services"}:
            raise AssertionError(f"unexpected delete {namespace}/{plural}/{name}")
        if plural == "pods":
            self.pods = [
                pod for pod in self.pods if pod["metadata"]["name"] != name
            ]

    def patch_annotations(self, namespace, plural, name, annotations):
        self.calls.append(("patch", namespace, plural, name, dict(annotations)))
        return {}

    def deleted(self, plural: str) -> list[str]:
        return [
            call[3] for call in self.calls
            if call[0] == "delete" and call[2] == plural
        ]


class FakeStore:
    def __init__(self, live_rows: list[dict] | None = None, *, broken: bool = False):
        self.live_rows = live_rows or []
        self.broken = broken
        self.released: list[tuple[str, str]] = []
        self.stale_calls: list[int] = []
        self.idle_calls: list[int] = []
        self.idle_workspace_ids: list[str] = []

    def release_stale_pending_runtimes(self, older_than_seconds: int) -> int:
        if self.broken:
            raise StoreError("database is unavailable: refused")
        self.stale_calls.append(older_than_seconds)
        return 0

    def list_live_runtimes(self) -> list[dict]:
        if self.broken:
            raise StoreError("database is unavailable: refused")
        return [dict(row) for row in self.live_rows]

    def release_runtime(self, tenant: str, sandbox_id: str) -> bool:
        self.released.append((tenant, sandbox_id))
        return True

    def idle_workspaces(self, older_than_seconds: int) -> list[str]:
        if self.broken:
            raise StoreError("database is unavailable: refused")
        self.idle_calls.append(older_than_seconds)
        return list(self.idle_workspace_ids)


class WorkspaceOffline(RuntimeError):
    pass


class ObjectStoreBusy(RuntimeError):
    """Stands in for core.ObjectStoreBusy: retryable, and a RuntimeError subclass like the real one."""


def pod(
    sandbox_id: str,
    *,
    expires_at: int | None,
    hard_expires_at: int | None = None,
    tenant: str | None = "acme",
    workspace_id: str | None = "ws-aaaaaaaaaaaa",
) -> dict:
    labels = {
        "app.kubernetes.io/name": "sandbox-runtime",
        "convee.io/sandbox-id": sandbox_id,
    }
    if workspace_id:
        labels["convee.io/workspace-id"] = workspace_id
    if tenant:
        labels["convee.io/tenant"] = tenant
    annotations = {}
    if expires_at is not None:
        annotations["convee.io/expires-at"] = str(expires_at)
    if hard_expires_at is not None:
        annotations["convee.io/hard-expires-at"] = str(hard_expires_at)
    return {
        "metadata": {
            "name": f"sandbox-{sandbox_id}",
            "labels": labels,
            "annotations": annotations,
        }
    }


def runtime_from_pod(item: dict) -> RuntimeInstance:
    metadata = item.get("metadata", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})

    def positive_int(name: str) -> int | None:
        try:
            value = int(annotations.get(name, ""))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    runtime_id = labels.get("convee.io/sandbox-id", "")
    return RuntimeInstance(
        runtime_id=runtime_id,
        workspace_id=labels.get("convee.io/workspace-id", ""),
        provider_id=metadata.get("name", runtime_id),
        state="running",
        ready=True,
        isolation="gvisor",
        tenant_id=labels.get("convee.io/tenant"),
        expires_at=positive_int("convee.io/expires-at"),
        hard_expires_at=positive_int("convee.io/hard-expires-at"),
    )


def build_control_plane(kube: FakeKube, store: FakeStore | None, *, busy: set[str] = frozenset()):
    control_plane = types.ModuleType("control_plane")
    control_plane.KUBE = kube
    control_plane.STORE = store
    control_plane.WORKLOAD_NAMESPACE = NAMESPACE
    control_plane.PENDING_STALE_SECONDS = 600
    control_plane.WORKSPACE_IDLE_TTL_SECONDS = 86_400
    control_plane.WorkspaceOffline = WorkspaceOffline
    control_plane.RuntimeDriverError = RuntimeDriverError
    control_plane.ObjectStoreBusy = ObjectStoreBusy
    control_plane.touched: list[tuple[str, int]] = []
    control_plane.deleted_runtimes: list[str] = []
    control_plane.forgotten: list[str] = []
    control_plane.probed: list[str] = []
    control_plane.runtime_pod_name = lambda sandbox_id: f"sandbox-{sandbox_id}"

    class RuntimeDriver:
        def list_runtimes(self):
            try:
                items = kube.list(
                    NAMESPACE,
                    "pods",
                    label_selector="app.kubernetes.io/name=sandbox-runtime",
                )
            except KubeError as exc:
                raise RuntimeDriverError(
                    RuntimeDriverErrorCode.UNAVAILABLE,
                    str(exc),
                    status=int(exc.status),
                ) from exc
            return [runtime_from_pod(item) for item in items]

        def delete_endpoint(self, sandbox_id):
            kube.delete(
                NAMESPACE,
                "services",
                control_plane.runtime_pod_name(sandbox_id),
            )

    runtime_driver = RuntimeDriver()
    control_plane.configured_runtime_driver = lambda: runtime_driver

    def probe_runtime_busy(sandbox_id: str) -> bool:
        control_plane.probed.append(sandbox_id)
        return sandbox_id in busy

    def touch_runtime(sandbox_id: str, now: int | None = None) -> dict:
        control_plane.touched.append((sandbox_id, now))
        return {}

    def delete_runtime(sandbox_id: str) -> None:
        # Mirror control_plane.delete_runtime: Service first, then Pod, then the row.
        name = control_plane.runtime_pod_name(sandbox_id)
        kube.delete(NAMESPACE, "services", name)
        kube.delete(NAMESPACE, "pods", name)
        control_plane.deleted_runtimes.append(sandbox_id)

    def volume_agent_request(method, path, query=None, timeout=None):
        raise WorkspaceOffline("volume role unavailable in this test")

    control_plane.probe_runtime_busy = probe_runtime_busy
    control_plane.touch_runtime = touch_runtime
    control_plane.delete_runtime = delete_runtime
    control_plane.volume_agent_request = volume_agent_request
    control_plane.forget_workspace_row = lambda workspace_id, owner=None: control_plane.forgotten.append(workspace_id)
    return control_plane


def load_reaper(control_plane_module: types.ModuleType):
    saved_core = sys.modules.get("control_plane.core")
    saved_reaper = sys.modules.pop("control_plane.reaper", None)
    sys.modules["control_plane.core"] = control_plane_module
    try:
        module = importlib.import_module("control_plane.reaper")
    finally:
        if saved_core is None:
            sys.modules.pop("control_plane.core", None)
        else:
            sys.modules["control_plane.core"] = saved_core
        if saved_reaper is not None:
            sys.modules["control_plane.reaper"] = saved_reaper
        else:
            sys.modules.pop("control_plane.reaper", None)
    return module


class ReaperCase(unittest.TestCase):
    def sweep(self, pods: list[dict], store: FakeStore | None, *, busy: set[str] = frozenset()):
        kube = FakeKube(pods)
        control_plane = build_control_plane(kube, store, busy=busy)
        reaper = load_reaper(control_plane)
        with contextlib.redirect_stdout(io.StringIO()):
            result = reaper.reap_once(now=NOW)
        return result, kube, control_plane


class TtlReapingTests(ReaperCase):
    def test_expired_runtime_is_deleted_and_fresh_one_survives(self) -> None:
        pods = [
            pod("sb-old", expires_at=NOW - 1),
            pod("sb-exact", expires_at=NOW),
            pod("sb-new", expires_at=NOW + 60),
        ]
        result, kube, control_plane = self.sweep(pods, store=None)
        self.assertEqual(result["runtimes"], 2)
        self.assertEqual(sorted(control_plane.deleted_runtimes), ["sb-exact", "sb-old"])
        self.assertEqual(
            sorted(kube.deleted("pods")), ["sandbox-sb-exact", "sandbox-sb-old"]
        )
        self.assertEqual([p["metadata"]["name"] for p in kube.pods], ["sandbox-sb-new"])
        self.assertEqual(control_plane.touched, [])

    def test_missing_or_garbled_expiry_never_deletes(self) -> None:
        pods = [
            pod("sb-none", expires_at=None),
            pod("sb-zero", expires_at=0),
        ]
        pods.append(pod("sb-garbage", expires_at=NOW - 1))
        pods[-1]["metadata"]["annotations"]["convee.io/expires-at"] = "soon"
        result, kube, control_plane = self.sweep(pods, store=None)
        self.assertEqual(result["runtimes"], 0)
        self.assertEqual(kube.deleted("pods"), [])
        self.assertEqual(control_plane.deleted_runtimes, [])

    def test_busy_runtime_is_reprieved_until_the_hard_limit(self) -> None:
        pods = [
            pod("sb-busy", expires_at=NOW - 1, hard_expires_at=NOW + 3600),
            pod("sb-hard", expires_at=NOW - 1, hard_expires_at=NOW - 1),
        ]
        result, kube, control_plane = self.sweep(
            pods, store=None, busy={"sb-busy", "sb-hard"}
        )
        self.assertEqual(result["reprieved"], 1)
        self.assertEqual(result["runtimes"], 1)
        self.assertEqual(control_plane.touched, [("sb-busy", NOW)])
        self.assertEqual(control_plane.deleted_runtimes, ["sb-hard"])
        # The hard limit is enforced without asking the runtime at all.
        self.assertEqual(control_plane.probed, ["sb-busy"])
        self.assertEqual(kube.deleted("pods"), ["sandbox-sb-hard"])

    def test_ttl_reaping_continues_when_the_store_is_unavailable(self) -> None:
        pods = [pod("sb-old", expires_at=NOW - 1), pod("sb-new", expires_at=NOW + 60)]
        store = FakeStore(broken=True)
        result, kube, control_plane = self.sweep(pods, store)
        self.assertEqual(result["runtimes"], 1)
        self.assertEqual(control_plane.deleted_runtimes, ["sb-old"])
        self.assertEqual(result["reconciled_rows"], 0)
        self.assertEqual(result["reconciled_orphans"], 0)
        self.assertEqual(store.released, [])


class ReconciliationTests(ReaperCase):
    def test_active_row_without_a_pod_releases_its_service_and_slot(self) -> None:
        store = FakeStore([
            {"tenant": "acme", "sandbox_id": "sb-gone", "workspace_id": "ws-1",
             "status": "active", "created_at": None},
            {"tenant": "acme", "sandbox_id": "sb-here", "workspace_id": "ws-1",
             "status": "active", "created_at": None},
            {"tenant": "acme", "sandbox_id": "sb-building", "workspace_id": "ws-1",
             "status": "pending", "created_at": None},
        ])
        pods = [pod("sb-here", expires_at=NOW + 60)]
        result, kube, control_plane = self.sweep(pods, store)
        self.assertEqual(result["reconciled_rows"], 1)
        self.assertEqual(store.released, [("acme", "sb-gone")])
        # Only the orphaned Service is deleted; live pods are untouched.
        self.assertEqual(kube.deleted("services"), ["sandbox-sb-gone"])
        self.assertEqual(kube.deleted("pods"), [])
        self.assertEqual(control_plane.deleted_runtimes, [])
        self.assertEqual(store.stale_calls, [600])

    def test_pod_with_tenant_label_but_no_live_row_is_an_orphan(self) -> None:
        store = FakeStore([
            {"tenant": "acme", "sandbox_id": "sb-known", "workspace_id": "ws-1",
             "status": "active", "created_at": None},
        ])
        pods = [
            pod("sb-known", expires_at=NOW + 60),
            pod("sb-orphan", expires_at=NOW + 60),
            pod("sb-legacy", expires_at=NOW + 60, tenant=None),
        ]
        result, kube, control_plane = self.sweep(pods, store)
        self.assertEqual(result["reconciled_orphans"], 1)
        self.assertEqual(control_plane.deleted_runtimes, ["sb-orphan"])
        self.assertEqual(kube.deleted("pods"), ["sandbox-sb-orphan"])
        self.assertEqual(result["reconciled_rows"], 0)
        self.assertEqual(store.released, [])

    def test_expired_pod_is_not_counted_twice_as_orphan(self) -> None:
        store = FakeStore([])
        pods = [pod("sb-old", expires_at=NOW - 1)]
        result, kube, control_plane = self.sweep(pods, store)
        self.assertEqual(result["runtimes"], 1)
        self.assertEqual(result["reconciled_orphans"], 0)
        self.assertEqual(control_plane.deleted_runtimes, ["sb-old"])

    def test_reconciliation_uses_a_fresh_pod_snapshot(self) -> None:
        store = FakeStore([
            {"tenant": "acme", "sandbox_id": "sb-x", "workspace_id": "ws-1",
             "status": "active", "created_at": None},
        ])
        pods = [pod("sb-x", expires_at=NOW + 60)]
        result, kube, control_plane = self.sweep(pods, store)
        self.assertEqual(kube.list_calls, 2)
        self.assertEqual(store.released, [])

    def test_reconciliation_is_skipped_when_the_fresh_list_fails(self) -> None:
        store = FakeStore([
            {"tenant": "acme", "sandbox_id": "sb-gone", "workspace_id": "ws-1",
             "status": "active", "created_at": None},
        ])

        class FlakyKube(FakeKube):
            def list(self, namespace, plural, label_selector=None):
                if self.list_calls >= 1:
                    self.calls.append(("list", namespace, plural, label_selector))
                    raise KubeError(HTTPStatus.SERVICE_UNAVAILABLE, "apiserver busy")
                return super().list(namespace, plural, label_selector)

        kube = FlakyKube([pod("sb-other", expires_at=NOW + 60)])
        control_plane = build_control_plane(kube, store)
        with contextlib.redirect_stdout(io.StringIO()):
            result = load_reaper(control_plane).reap_once(now=NOW)
        self.assertEqual(result["reconciled_rows"], 0)
        self.assertEqual(store.released, [])
        self.assertNotIn("sandbox-sb-gone", kube.deleted("services"))
        # Direction two (orphans) does not need the fresh snapshot.
        self.assertEqual(result["reconciled_orphans"], 1)
        self.assertEqual(control_plane.deleted_runtimes, ["sb-other"])


class WorkspaceSweepTests(ReaperCase):
    def test_workspace_sweep_failure_does_not_block_runtime_reaping(self) -> None:
        result, kube, control_plane = self.sweep([pod("sb-old", expires_at=NOW - 1)], None)
        self.assertEqual(result["workspaces"], 0)
        self.assertEqual(result["runtimes"], 1)
        self.assertEqual(control_plane.forgotten, [])

    def test_idle_workspace_without_a_runtime_is_removed(self) -> None:
        """The idle verdict comes from the store; a live Runtime still protects it.

        🔴 It must not come from the volume. `.sandbox/last_used_at` sits inside the
        tenant's own writable tree under the uid the shell runs as, so reading it let
        a tenant make its Workspace unreclaimable with one `rm -rf` - and because the
        global cap counts directories on the volume, filling it turned every other
        tenant's create into a 429. This test forges that marker in the worst possible
        way and asserts the sweep does not care."""
        kube = FakeKube([pod("sb-live", expires_at=NOW + 60, workspace_id="ws-active")])
        store = FakeStore()
        store.idle_workspace_ids = ["ws-active", "ws-idle"]
        control_plane = build_control_plane(kube, store)
        removals: list[tuple] = []

        def volume_agent_request(method, path, query=None, timeout=None):
            if method == "GET" and path == "/v1/workspaces":
                #Forged exactly the way a tenant would: the marker is gone for the
                #Workspace that must be collected, and freshly bumped for one that
                #must not be. Neither may change the outcome.
                listing = {
                    "workspaces": [
                        {"id": "ws-active", "last_used_at": NOW - 10 ** 6},
                        {"id": "ws-idle", "last_used_at": None},
                        {"id": "ws-fresh", "last_used_at": NOW - 10},
                    ]
                }
                return 200, json.dumps(listing), {}
            if method == "DELETE":
                removals.append((path, query, timeout))
                return 200, "{}", {}
            raise AssertionError(f"unexpected volume request {method} {path}")

        control_plane.volume_agent_request = volume_agent_request
        with contextlib.redirect_stdout(io.StringIO()):
            result = load_reaper(control_plane).reap_once(now=NOW)
        self.assertEqual(store.idle_calls, [control_plane.WORKSPACE_IDLE_TTL_SECONDS])
        self.assertEqual(result["workspaces"], 1)
        self.assertEqual(removals, [("/v1/workspaces/ws-idle", {"remove": "1"}, 120)])
        self.assertEqual(control_plane.forgotten, ["ws-idle"])

    def test_a_workspace_whose_runtime_died_this_round_is_not_swept(self) -> None:
        """The round that deletes an expired Runtime must not also delete its Workspace.

        Before this, delete-by-TTL did not add the Workspace to active_workspaces, and
        the idle sweep in the same round read a column only admission had written: a
        client that talked to its Runtime for hours lost the Workspace in the same 15s
        round the Runtime hit the hard TTL. One round of grace; next round the store
        column (refreshed by delete_runtime's touch) decides as usual."""
        kube = FakeKube([
            pod("sb-dead", expires_at=NOW - 1, workspace_id="ws-dying"),
        ])
        store = FakeStore()
        store.idle_workspace_ids = ["ws-dying", "ws-idle"]
        control_plane = build_control_plane(kube, store)
        removals: list[str] = []

        def volume_agent_request(method, path, query=None, timeout=None):
            if method == "DELETE":
                removals.append(path)
                return 200, "{}", {}
            raise AssertionError(f"unexpected volume request {method} {path}")

        control_plane.volume_agent_request = volume_agent_request
        with contextlib.redirect_stdout(io.StringIO()):
            result = load_reaper(control_plane).reap_once(now=NOW)
        self.assertEqual(control_plane.deleted_runtimes, ["sb-dead"])
        self.assertEqual(result["runtimes"], 1)
        self.assertEqual(removals, ["/v1/workspaces/ws-idle"])
        self.assertEqual(control_plane.forgotten, ["ws-idle"])
        self.assertEqual(result["workspaces"], 1)

    def test_a_workspace_the_store_does_not_call_idle_is_never_swept(self) -> None:
        """A recently touched Workspace is absent from idle_workspaces; the sweep has no
        second opinion. The volume marker says "ancient" here and must not matter."""
        kube = FakeKube([])
        store = FakeStore()
        store.idle_workspace_ids = []
        control_plane = build_control_plane(kube, store)
        removals: list[str] = []

        def volume_agent_request(method, path, query=None, timeout=None):
            if method == "GET" and path == "/v1/workspaces":
                return 200, json.dumps({"workspaces": [
                    {"id": "ws-touched", "last_used_at": NOW - 10 ** 6},
                ]}), {}
            if method == "DELETE":
                removals.append(path)
                return 200, "{}", {}
            raise AssertionError(f"unexpected volume request {method} {path}")

        control_plane.volume_agent_request = volume_agent_request
        with contextlib.redirect_stdout(io.StringIO()):
            result = load_reaper(control_plane).reap_once(now=NOW)
        self.assertEqual(store.idle_calls, [control_plane.WORKSPACE_IDLE_TTL_SECONDS])
        self.assertEqual(removals, [])
        self.assertEqual(result["workspaces"], 0)


class CheckpointSweepTests(ReaperCase):
    """The checkpoint GC walks the bucket page by page and deletes as it goes.

    It used to call object_list on the whole bucket, which refuses past
    MAX_LIST_ENTRIES (10000). Once the bucket held more checkpoint objects than
    that, every round raised before the first delete, and nothing else ever
    removed them: the sweep stopped for good. Here the fake bucket holds more
    than the ceiling, and the sweep must still delete every expired archive.
    """

    CEILING = 10_000
    RETENTION = 3_600

    def sweep_checkpoints(self, objects: list[dict], *, page_size: int = 1000) -> tuple[int, list[str], int]:
        kube = FakeKube([])
        control_plane = build_control_plane(kube, FakeStore())
        control_plane.OBJECT_STORE_WORKSPACE_BUCKET = "workspaces"
        control_plane.CHECKPOINT_RETENTION_SECONDS = self.RETENTION
        control_plane.MAX_LIST_ENTRIES = self.CEILING
        deleted: list[str] = []
        pages_served = 0

        def object_list(bucket, prefix):
            raise AssertionError("the sweep must not list the whole bucket at once")

        def object_list_page(bucket, prefix, *, continuation_token=None, page_size=page_size):
            nonlocal pages_served
            pages_served += 1
            start = int(continuation_token or 0)
            page = objects[start:start + page_size]
            next_token = str(start + page_size) if start + page_size < len(objects) else None
            return page, next_token

        control_plane.object_list = object_list
        control_plane.object_list_page = object_list_page
        control_plane.object_delete_versions = lambda bucket, key: deleted.append(key)
        reaper = load_reaper(control_plane)
        removed = reaper.reap_expired_checkpoints(now=NOW)
        return removed, deleted, pages_served

    @staticmethod
    def archive(index: int, *, age: int) -> dict:
        from datetime import datetime, timezone
        modified = datetime.fromtimestamp(NOW - age, tz=timezone.utc).isoformat()
        return {
            "key": f"workspaces/ws-{index:012x}/checkpoints/cp-{index}.tar.gz",
            "bytes": 1,
            "last_modified": modified,
        }

    def test_a_bucket_past_the_listing_ceiling_is_still_swept(self) -> None:
        expired = [self.archive(i, age=self.RETENTION + 1) for i in range(self.CEILING + 500)]
        fresh = [self.archive(10 ** 6 + i, age=10) for i in range(3)]
        other = [{"key": "workspaces/ws-000000000000/data/not-a-checkpoint.tar.gz",
                  "bytes": 1, "last_modified": "2020-01-01T00:00:00+00:00"}]
        removed, deleted, pages = self.sweep_checkpoints(expired + fresh + other)
        self.assertEqual(removed, len(expired))
        self.assertEqual(sorted(deleted), sorted(item["key"] for item in expired))
        self.assertGreater(pages, 1, "the walk must be paged, not one listing")

    def test_an_empty_bucket_is_one_page_and_no_deletes(self) -> None:
        removed, deleted, pages = self.sweep_checkpoints([])
        self.assertEqual((removed, deleted, pages), (0, [], 1))

    def sweep_with_purge(self, purge, *, objects: list[dict]) -> tuple:
        kube = FakeKube([])
        control_plane = build_control_plane(kube, FakeStore())
        control_plane.OBJECT_STORE_WORKSPACE_BUCKET = "workspaces"
        control_plane.CHECKPOINT_RETENTION_SECONDS = self.RETENTION
        plain: list[str] = []
        control_plane.object_list_page = lambda bucket, prefix, *, continuation_token=None: (objects, None)
        control_plane.object_delete_versions = purge
        control_plane.object_delete = lambda bucket, key: plain.append(key)
        reaper = load_reaper(control_plane)
        with contextlib.redirect_stdout(io.StringIO()):
            removed = reaper.reap_expired_checkpoints(now=NOW)
        return removed, plain

    def test_a_store_without_versioning_still_gets_its_checkpoints_swept(self) -> None:
        """The versioned purge is rejected there; the sweep falls back to a plain delete
        instead of stopping at its first expired object, as it did before."""
        expired = [self.archive(i, age=self.RETENTION + 1) for i in range(3)]

        def rejected(bucket, key):
            raise RuntimeError("object storage rejected the operation")

        removed, plain = self.sweep_with_purge(rejected, objects=expired)
        self.assertEqual(removed, 3)
        self.assertEqual(plain, [item["key"] for item in expired])

    def test_an_outage_during_the_sweep_is_not_papered_over(self) -> None:
        expired = [self.archive(i, age=self.RETENTION + 1) for i in range(3)]

        def unreachable(bucket, key):
            raise ObjectStoreBusy("object storage is unreachable; retry shortly")

        with self.assertRaises(ObjectStoreBusy):
            self.sweep_with_purge(unreachable, objects=expired)


def load_admission_helpers(kube: FakeKube, *, ttl: int, idle_evict: int, now: float) -> dict:
    """Extract idle-victim selection and provider-neutral Runtime admission.

    ``control_plane`` cannot be imported here (it reads its environment and opens a
    Kubernetes client at import), so the two functions are compiled from the
    source with their module-level collaborators supplied by the test.
    """
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_idle_runtime_victims", "_admit_new_runtime"}
    body = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in body} == wanted

    class Rejections:
        def __init__(self) -> None:
            self.gates: list[str] = []

        def inc(self, *, gate: str) -> None:
            self.gates.append(gate)

    class RuntimeDriver:
        def list_runtimes(self):
            items = kube.list(
                NAMESPACE,
                "pods",
                label_selector=RUNTIME_SELECTOR,
            )
            return [runtime_from_pod(item) for item in items]

        def delete_runtime(self, runtime_id):
            name = f"sandbox-{runtime_id}"
            kube.delete(NAMESPACE, "services", name)
            kube.delete(NAMESPACE, "pods", name)

    driver = RuntimeDriver()
    namespace = {
        "configured_runtime_driver": lambda: driver,
        "SANDBOX_TTL_SECONDS": ttl,
        "SANDBOX_IDLE_EVICT_SECONDS": idle_evict,
        "QUOTA_REJECTIONS": Rejections(),
        "KubeError": KubeError,
        "RuntimeInstance": RuntimeInstance,
        "HTTPStatus": HTTPStatus,
        "time": types.SimpleNamespace(time=lambda: now),
        "print": lambda *args, **kwargs: None,
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "core.py", "exec"),
        namespace,
    )
    return namespace


class IdleEvictionTests(unittest.TestCase):
    TTL = 1800
    IDLE_EVICT = 600

    def helpers(self, pods: list[dict]) -> tuple[dict, FakeKube]:
        kube = FakeKube(pods)
        return load_admission_helpers(kube, ttl=self.TTL, idle_evict=self.IDLE_EVICT, now=NOW), kube

    def test_idle_victims_are_the_pods_untouched_past_the_cutoff(self) -> None:
        idle_cutoff = NOW - self.IDLE_EVICT
        # last_touch = expires_at - TTL; idle when last_touch < cutoff.
        pods = [
            pod("sb-idle", expires_at=idle_cutoff + self.TTL - 1),
            pod("sb-edge", expires_at=idle_cutoff + self.TTL),
            pod("sb-active", expires_at=NOW + self.TTL),
            pod("sb-unknown", expires_at=None),
        ]
        helpers, _ = self.helpers(pods)
        victims = helpers["_idle_runtime_victims"](
            [runtime_from_pod(item) for item in pods], idle_cutoff
        )
        self.assertEqual([v.provider_id for v in victims], ["sandbox-sb-idle"])

    def test_full_pool_evicts_idle_runtimes_before_admitting(self) -> None:
        pods = [
            pod("sb-idle", expires_at=NOW - self.IDLE_EVICT + self.TTL - 1),
            pod("sb-active", expires_at=NOW + self.TTL),
        ]
        helpers, kube = self.helpers(pods)
        helpers["_admit_new_runtime"](2)
        self.assertEqual(kube.deleted("pods"), ["sandbox-sb-idle"])
        self.assertEqual(kube.deleted("services"), ["sandbox-sb-idle"])
        self.assertEqual(kube.list_calls, 2)
        self.assertEqual(helpers["QUOTA_REJECTIONS"].gates, [])

    def test_full_pool_without_idle_runtimes_is_rejected_with_429(self) -> None:
        pods = [
            pod("sb-a", expires_at=NOW + self.TTL),
            pod("sb-b", expires_at=NOW + self.TTL),
        ]
        helpers, kube = self.helpers(pods)
        with self.assertRaises(KubeError) as raised:
            helpers["_admit_new_runtime"](2)
        self.assertEqual(raised.exception.status, HTTPStatus.TOO_MANY_REQUESTS)
        self.assertIn("transient", str(raised.exception))
        self.assertEqual(kube.deleted("pods"), [])
        self.assertEqual(helpers["QUOTA_REJECTIONS"].gates, ["global"])

    def test_pool_with_room_admits_without_touching_pods(self) -> None:
        pods = [pod("sb-idle", expires_at=NOW - 10 ** 6)]
        helpers, kube = self.helpers(pods)
        helpers["_admit_new_runtime"](2)
        self.assertEqual(kube.deleted("pods"), [])
        self.assertEqual(kube.list_calls, 1)


if __name__ == "__main__":
    unittest.main()
