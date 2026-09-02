"""``ensure_runtime`` reuse and the Runtime's expiry (control_plane/core.py).

A Workspace's existing Runtime is reused rather than provisioned twice, and
until 2026-09-02 the reuse did not look at ``expires_at``: a Runtime the
reaper had already judged expired in its snapshot was touched and handed
out, and the reaper's delete - already past its own check - landed seconds
later. The client's next MCP call met a 502.

``ensure_runtime`` cannot be imported (``core.py`` reads its environment and
builds a Kubernetes client at import), so the function is compiled from the
source with every collaborator supplied here, the same way
``test_reaper_behavior`` loads the admission helpers.
"""
from __future__ import annotations

import ast
import contextlib
import pathlib
import sys
import threading
import types
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
from control_plane.kube import KubeError  # noqa: E402
from control_plane.store import StoreError  # noqa: E402

NOW = 1_700_000_000
WORKSPACE = "ws-aaaaaaaaaaaa"


def instance(runtime_id: str, *, expires_at: int | None, hard_expires_at: int | None = None) -> RuntimeInstance:
    return RuntimeInstance(
        runtime_id=runtime_id,
        workspace_id=WORKSPACE,
        provider_id=f"sandbox-{runtime_id}",
        state="running",
        ready=True,
        isolation="gvisor",
        expires_at=expires_at,
        hard_expires_at=hard_expires_at,
    )


class FakeDriver:
    def __init__(self, existing: list[RuntimeInstance], *, gone_after_touch: set[str] = frozenset()):
        self.existing = existing
        self.gone_after_touch = gone_after_touch
        self.touched: list[str] = []
        self.created: list[str] = []
        self.alive: dict[str, RuntimeInstance] = {item.runtime_id: item for item in existing}

    def list_for_workspace(self, workspace_id: str) -> list[RuntimeInstance]:
        return list(self.existing)

    def touch_runtime(self, runtime_id: str, expires_at: int) -> RuntimeInstance:
        self.touched.append(runtime_id)
        if runtime_id in self.gone_after_touch:
            self.alive.pop(runtime_id, None)
            raise RuntimeDriverError(RuntimeDriverErrorCode.NOT_FOUND, "gone", status=404)
        refreshed = RuntimeInstance(**{**self.alive[runtime_id].__dict__, "expires_at": expires_at})
        self.alive[runtime_id] = refreshed
        return refreshed

    def create_runtime(self, spec: RuntimeSpec) -> None:
        self.created.append(spec.runtime_id)
        self.alive[spec.runtime_id] = instance(spec.runtime_id, expires_at=NOW + 1800)

    def ensure_endpoint(self, runtime_id: str) -> None:
        pass

    def endpoint(self, runtime_id: str) -> str:
        return f"http://{runtime_id}"


def load_ensure_runtime(driver: FakeDriver) -> tuple:
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "ensure_runtime"
    ]
    assert len(body) == 1

    class Metric:
        def observe(self, value): pass
        def inc(self, **labels): pass

    touched_workspaces: list[str] = []

    def touch_runtime(runtime_id: str, now: int | None = None) -> RuntimeInstance:
        # Mirrors core.touch_runtime: the Workspace clock is refreshed from here.
        refreshed = driver.touch_runtime(runtime_id, (now or NOW) + 1800)
        touched_workspaces.append(refreshed.workspace_id)
        return refreshed

    namespace = {
        "configured_runtime_driver": lambda: driver,
        "RuntimeSpec": RuntimeSpec,
        "RuntimeInstance": RuntimeInstance,
        "RuntimeDriverError": RuntimeDriverError,
        "RuntimeDriverErrorCode": RuntimeDriverErrorCode,
        "runtime_state_key": lambda tenant_id, limit: "state",
        "time": types.SimpleNamespace(time=lambda: NOW, monotonic=lambda: 0.0),
        "_RUNTIME_ADMISSION_LOCK": threading.Lock(),
        "wait_for_runtime": lambda runtime_id, timeout=90.0: driver.alive[runtime_id],
        "touch_runtime": touch_runtime,
        "runtime_exists": lambda runtime_id: driver.alive.get(runtime_id),
        "_admit_new_runtime": lambda maximum: None,
        "reserve_runtime_state": lambda *args, **kwargs: None,
        "activate_runtime_state": lambda key, runtime_id: True,
        "release_runtime_state": lambda *args, **kwargs: None,
        "runtime_create_phase": lambda name: contextlib.nullcontext(),
        "capability_ticket_for": lambda kind, subject: "ticket",
        "wait_for_internal_health": lambda url, ticket: None,
        "RUNTIME_CREATE_SECONDS": Metric(),
        "RUNTIME_CREATE_FAILURES": Metric(),
        "create_failure_reason": lambda exc: "test",
        "delete_runtime": lambda runtime_id: None,
        "touch_workspace": touched_workspaces.append,
        "MAX_RUNTIMES": 4,
        "KubeError": KubeError,
        "HTTPStatus": HTTPStatus,
        "StoreError": StoreError,
        "contextlib": contextlib,
        "print": lambda *args, **kwargs: None,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "core.py", "exec"), namespace)
    return namespace["ensure_runtime"], touched_workspaces


class RuntimeReuseTests(unittest.TestCase):
    def test_a_live_runtime_is_reused_and_touched(self) -> None:
        driver = FakeDriver([instance("sb-live", expires_at=NOW + 600)])
        ensure_runtime, touched_workspaces = load_ensure_runtime(driver)
        result = ensure_runtime("sb-new", WORKSPACE)
        self.assertEqual(result.runtime_id, "sb-live")
        self.assertEqual(driver.touched, ["sb-live"])
        self.assertEqual(driver.created, [])
        self.assertEqual(touched_workspaces, [WORKSPACE])

    def test_a_runtime_past_its_hard_ceiling_is_not_handed_out(self) -> None:
        # The reaper deletes it within a round no matter what; a touch does not
        # move the ceiling. Provision a new one instead.
        driver = FakeDriver([instance("sb-old", expires_at=NOW + 600, hard_expires_at=NOW - 1)])
        ensure_runtime, _ = load_ensure_runtime(driver)
        result = ensure_runtime("sb-new", WORKSPACE)
        self.assertEqual(result.runtime_id, "sb-new")
        self.assertEqual(driver.created, ["sb-new"])
        self.assertNotIn("sb-old", driver.touched)

    def test_an_idle_expired_runtime_is_reused_only_if_it_survives_the_touch(self) -> None:
        driver = FakeDriver([instance("sb-expired", expires_at=NOW - 1)])
        ensure_runtime, _ = load_ensure_runtime(driver)
        result = ensure_runtime("sb-new", WORKSPACE)
        self.assertEqual(result.runtime_id, "sb-expired")
        # The instance handed out is the re-read one, carrying the new expiry.
        self.assertEqual(result.expires_at, NOW + 1800)
        self.assertEqual(driver.created, [])

    def test_an_idle_expired_runtime_the_reaper_already_deleted_is_replaced(self) -> None:
        driver = FakeDriver([instance("sb-expired", expires_at=NOW - 1)], gone_after_touch={"sb-expired"})
        ensure_runtime, _ = load_ensure_runtime(driver)
        result = ensure_runtime("sb-new", WORKSPACE)
        self.assertEqual(result.runtime_id, "sb-new")
        self.assertEqual(driver.touched, ["sb-expired"])
        self.assertEqual(driver.created, ["sb-new"])


if __name__ == "__main__":
    unittest.main()
