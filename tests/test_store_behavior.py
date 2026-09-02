"""Behavioral invariants of the control-plane store (control_plane/store.py).

Every test runs against a throwaway SQLite database and only uses the public
``Store`` API. The invariants pinned here are the ones a one-line source
mutation would silently break while the rest of the suite stays green:

* runtime status changes are compare-and-swap (``_transition`` keeps its
  ``AND status IN (...)`` guard) and terminal states never move again;
* ``admit_runtime`` refuses the (max_runtimes + 1)-th live runtime and leaves
  the live count untouched;
* ``release_stale_pending_runtimes`` only touches pending rows older than the
  threshold;
* workspace quota admission is idempotent for the same workspace, refuses at
  capacity, and refuses to move a workspace between tenants.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


def load_store_module():
    path = Path(__file__).resolve().parents[1] / "control_plane/store.py"
    spec = importlib.util.spec_from_file_location("sandbox_store_behavior", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


store_module = load_store_module()
Store = store_module.Store
StoreError = store_module.StoreError
WORKSPACE_ADMITTED = store_module.WORKSPACE_ADMITTED
WORKSPACE_REUSED = store_module.WORKSPACE_REUSED
WORKSPACE_AT_CAPACITY = store_module.WORKSPACE_AT_CAPACITY

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
WORKSPACE = "ws-aaaaaaaaaaaa"
TEMPLATE = "default"


class StoreCase(unittest.TestCase):
    max_runtimes = 2
    max_workspaces = 2

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary_directory.name) / "store.sqlite3"
        self.store = Store.sqlite(self.path)
        self.store.ensure_schema()
        for tenant in (TENANT, OTHER_TENANT):
            self.store.create_tenant(
                tenant,
                f"Tenant {tenant}",
                max_workspaces=self.max_workspaces,
                max_runtimes=self.max_runtimes,
            )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def admit(self, sandbox_id: str, tenant: str = TENANT) -> bool:
        return self.store.admit_runtime(
            tenant, sandbox_id, WORKSPACE, TEMPLATE, self.max_runtimes
        )

    def status_of(self, sandbox_id: str) -> str | None:
        row = self.store.get_runtime(sandbox_id)
        return None if row is None else row["status"]

    def live_ids(self) -> set[str]:
        return {row["sandbox_id"] for row in self.store.list_live_runtimes()}

    def backdate_runtime(self, sandbox_id: str, seconds: int) -> None:
        """Move ``updated_at`` into the past without going through the Store.

        The store closes its connection after every call, so a second sqlite3
        connection on the same file does not contend with it.
        """
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE sandbox_runtimes SET updated_at = "
                "datetime('now', ?) WHERE sandbox_id = ?",
                (f"-{seconds} seconds", sandbox_id),
            )
            connection.commit()


class RuntimeTransitionTests(StoreCase):
    def test_pending_runtime_activates_exactly_once(self) -> None:
        self.assertTrue(self.admit("sb-1"))
        self.assertEqual(self.status_of("sb-1"), "pending")
        self.assertTrue(self.store.activate_runtime(TENANT, "sb-1"))
        self.assertEqual(self.status_of("sb-1"), "active")
        # A second activation finds no pending row: 0 rows affected.
        self.assertFalse(self.store.activate_runtime(TENANT, "sb-1"))
        self.assertEqual(self.status_of("sb-1"), "active")

    def test_release_moves_live_rows_to_released(self) -> None:
        self.assertTrue(self.admit("sb-active"))
        self.assertTrue(self.store.activate_runtime(TENANT, "sb-active"))
        self.assertTrue(self.store.release_runtime(TENANT, "sb-active"))
        self.assertEqual(self.status_of("sb-active"), "released")

        self.assertTrue(self.admit("sb-pending"))
        self.assertTrue(
            self.store.release_runtime(TENANT, "sb-pending", failed=True)
        )
        self.assertEqual(self.status_of("sb-pending"), "failed")
        self.assertEqual(self.live_ids(), set())

    def test_terminal_states_never_move_again(self) -> None:
        self.assertTrue(self.admit("sb-done"))
        self.assertTrue(self.store.activate_runtime(TENANT, "sb-done"))
        self.assertTrue(self.store.release_runtime(TENANT, "sb-done"))

        # released -> active must be refused (CAS guard on the expected set).
        self.assertFalse(self.store.activate_runtime(TENANT, "sb-done"))
        self.assertEqual(self.status_of("sb-done"), "released")
        # released -> failed must be refused: repeated DELETE is idempotent.
        self.assertFalse(
            self.store.release_runtime(TENANT, "sb-done", failed=True)
        )
        self.assertEqual(self.status_of("sb-done"), "released")
        # released -> released is also a no-op.
        self.assertFalse(self.store.release_runtime(TENANT, "sb-done"))
        self.assertEqual(self.status_of("sb-done"), "released")
        self.assertIsNone(self.store.runtime_owner("sb-done"))

    def test_failed_rows_never_move_again(self) -> None:
        self.assertTrue(self.admit("sb-broken"))
        self.assertTrue(
            self.store.release_runtime(TENANT, "sb-broken", failed=True)
        )
        self.assertFalse(self.store.activate_runtime(TENANT, "sb-broken"))
        self.assertFalse(self.store.release_runtime(TENANT, "sb-broken"))
        self.assertEqual(self.status_of("sb-broken"), "failed")

    def test_transition_is_scoped_to_the_owning_tenant(self) -> None:
        self.assertTrue(self.admit("sb-owned"))
        self.assertFalse(self.store.activate_runtime(OTHER_TENANT, "sb-owned"))
        self.assertFalse(self.store.release_runtime(OTHER_TENANT, "sb-owned"))
        self.assertEqual(self.status_of("sb-owned"), "pending")
        self.assertEqual(self.store.runtime_owner("sb-owned"), TENANT)

    def test_transition_of_unknown_runtime_is_a_noop(self) -> None:
        self.assertFalse(self.store.activate_runtime(TENANT, "sb-missing"))
        self.assertFalse(self.store.release_runtime(TENANT, "sb-missing"))
        self.assertIsNone(self.store.get_runtime("sb-missing"))


class RuntimeAdmissionTests(StoreCase):
    def test_admission_stops_at_max_runtimes_and_keeps_the_count(self) -> None:
        self.assertTrue(self.admit("sb-1"))
        self.assertTrue(self.admit("sb-2"))
        self.assertEqual(self.store.count_all_live_runtimes(), 2)

        self.assertFalse(self.admit("sb-3"))
        self.assertIsNone(self.store.get_runtime("sb-3"))
        self.assertEqual(self.store.count_all_live_runtimes(), 2)
        self.assertEqual(self.live_ids(), {"sb-1", "sb-2"})
        # Rejection is stable, not a one-off.
        self.assertFalse(self.admit("sb-4"))
        self.assertEqual(self.store.count_all_live_runtimes(), 2)

    def test_active_runtimes_count_toward_the_quota(self) -> None:
        self.assertTrue(self.admit("sb-1"))
        self.assertTrue(self.store.activate_runtime(TENANT, "sb-1"))
        self.assertTrue(self.admit("sb-2"))
        self.assertFalse(self.admit("sb-3"))

    def test_released_slot_becomes_available_again(self) -> None:
        self.assertTrue(self.admit("sb-1"))
        self.assertTrue(self.admit("sb-2"))
        self.assertFalse(self.admit("sb-3"))
        self.assertTrue(self.store.release_runtime(TENANT, "sb-1"))
        self.assertTrue(self.admit("sb-3"))
        self.assertEqual(self.live_ids(), {"sb-2", "sb-3"})

    def test_failed_rows_do_not_occupy_a_slot(self) -> None:
        self.assertTrue(self.admit("sb-1"))
        self.assertTrue(self.store.release_runtime(TENANT, "sb-1", failed=True))
        self.assertTrue(self.admit("sb-2"))
        self.assertTrue(self.admit("sb-3"))
        self.assertFalse(self.admit("sb-4"))

    def test_quota_is_per_tenant(self) -> None:
        self.assertTrue(self.admit("sb-a1"))
        self.assertTrue(self.admit("sb-a2"))
        self.assertFalse(self.admit("sb-a3"))
        self.assertTrue(self.admit("sb-b1", OTHER_TENANT))
        self.assertTrue(self.admit("sb-b2", OTHER_TENANT))
        self.assertFalse(self.admit("sb-b3", OTHER_TENANT))
        self.assertEqual(self.store.count_all_live_runtimes(), 4)

    def test_limit_argument_is_honoured_not_the_row_count(self) -> None:
        self.assertTrue(
            self.store.admit_runtime(TENANT, "sb-1", WORKSPACE, TEMPLATE, 1)
        )
        self.assertFalse(
            self.store.admit_runtime(TENANT, "sb-2", WORKSPACE, TEMPLATE, 1)
        )
        self.assertTrue(
            self.store.admit_runtime(TENANT, "sb-2", WORKSPACE, TEMPLATE, 2)
        )

    def test_unknown_tenant_is_refused_without_a_row(self) -> None:
        with self.assertRaises(StoreError):
            self.store.admit_runtime("ghost", "sb-1", WORKSPACE, TEMPLATE, 5)
        self.assertIsNone(self.store.get_runtime("sb-1"))

    def test_untenanted_runtime_does_not_consume_tenant_quota(self) -> None:
        self.store.record_untenanted_runtime("sb-u", WORKSPACE, TEMPLATE)
        self.assertEqual(self.status_of("sb-u"), "pending")
        self.assertTrue(self.admit("sb-1"))
        self.assertTrue(self.admit("sb-2"))
        self.assertFalse(self.admit("sb-3"))


class StalePendingTests(StoreCase):
    def test_only_old_pending_rows_are_failed(self) -> None:
        self.assertTrue(self.admit("sb-stale"))
        self.assertTrue(self.admit("sb-fresh"))
        self.backdate_runtime("sb-stale", 7200)

        self.assertEqual(self.store.release_stale_pending_runtimes(3600), 1)
        self.assertEqual(self.status_of("sb-stale"), "failed")
        self.assertEqual(self.status_of("sb-fresh"), "pending")
        # The freed slot is usable again.
        self.assertTrue(self.admit("sb-next"))

    def test_active_rows_are_never_treated_as_stale(self) -> None:
        self.assertTrue(self.admit("sb-old-active"))
        self.assertTrue(self.store.activate_runtime(TENANT, "sb-old-active"))
        self.backdate_runtime("sb-old-active", 7200)
        self.assertEqual(self.store.release_stale_pending_runtimes(3600), 0)
        self.assertEqual(self.status_of("sb-old-active"), "active")

    def test_nothing_to_release_returns_zero(self) -> None:
        self.assertTrue(self.admit("sb-fresh"))
        self.assertEqual(self.store.release_stale_pending_runtimes(3600), 0)
        self.assertEqual(self.status_of("sb-fresh"), "pending")


class WorkspaceAdmissionTests(StoreCase):
    def admit_workspace(self, workspace_id: str, tenant: str = TENANT) -> str:
        return self.store.admit_workspace(
            tenant,
            workspace_id,
            principal_kind="user",
            principal_id="alice",
            session_key="session-1",
            limit=self.max_workspaces,
        )

    def test_same_workspace_is_reused_without_a_new_slot(self) -> None:
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_REUSED)
        self.assertEqual(self.store.count_workspaces(TENANT), 1)

    def test_capacity_is_reported_not_raised(self) -> None:
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-2"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-3"), WORKSPACE_AT_CAPACITY)
        self.assertEqual(self.store.count_workspaces(TENANT), 2)
        self.assertIsNone(self.store.owner_of("ws-3"))
        # Re-entering an existing workspace still works at capacity.
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_REUSED)

    def test_workspace_cannot_change_tenant(self) -> None:
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        with self.assertRaises(StoreError):
            self.admit_workspace("ws-1", OTHER_TENANT)
        self.assertEqual(self.store.owner_of("ws-1"), TENANT)
        self.assertEqual(self.store.count_workspaces(OTHER_TENANT), 0)

    def test_soft_delete_frees_the_slot_and_hides_ownership(self) -> None:
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-2"), WORKSPACE_ADMITTED)
        self.store.forget_workspace(TENANT, "ws-1")
        self.assertIsNone(self.store.owner_of("ws-1"))
        self.assertEqual(self.store.count_workspaces(TENANT), 1)
        self.assertEqual(self.admit_workspace("ws-3"), WORKSPACE_ADMITTED)

    def test_soft_deleted_workspace_can_be_registered_again(self) -> None:
        """Re-registering a forgotten workspace id must not fail.

        ``sandbox_workspaces`` keeps soft-deleted rows for audit, and its primary
        key is ``(tenant_id, workspace_id)``. The deterministic workspace id
        derivation means the same session legitimately comes back with the same
        id after its data was reclaimed, so the second registration has to
        succeed instead of tripping over the retained row.

        Regression guard for the 2026-08-19 review item 3: the conditional
        INSERT used to collide with the retained row (UNIQUE constraint), so the
        store now revives the soft-deleted row under the same quota gate.
        """
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.store.forget_workspace(TENANT, "ws-1")
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.store.owner_of("ws-1"), TENANT)
        self.assertEqual(self.store.count_workspaces(TENANT), 1)

    def test_reviving_a_soft_deleted_workspace_respects_the_quota(self) -> None:
        """Revival is an admission: at capacity it must be rejected like a fresh row."""
        self.max_workspaces = 1
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.store.forget_workspace(TENANT, "ws-1")
        self.assertEqual(self.admit_workspace("ws-2"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_AT_CAPACITY)
        self.assertIsNone(self.store.owner_of("ws-1"))
        self.store.forget_workspace(TENANT, "ws-2")
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.store.count_workspaces(TENANT), 1)

    def test_register_workspace_ignores_the_quota(self) -> None:
        self.assertEqual(self.admit_workspace("ws-1"), WORKSPACE_ADMITTED)
        self.assertEqual(self.admit_workspace("ws-2"), WORKSPACE_ADMITTED)
        self.store.register_workspace(
            TENANT,
            "ws-admin",
            principal_kind="admin",
            principal_id="ops",
            session_key="fixture",
        )
        self.assertEqual(self.store.owner_of("ws-admin"), TENANT)
        self.assertEqual(self.store.count_workspaces(TENANT), 3)


class WorkspaceTouchTests(StoreCase):
    """``touch_workspace`` is what keeps a Workspace out of the idle sweep.

    The reaper's verdict is ``idle_workspaces`` and nothing else, and that column
    used to be written by admission alone. These pin the two halves a mutation
    would break silently: a touch moves the clock (a recently touched Workspace
    is not a candidate), and the throttle means a touch inside the window
    changes nothing - the same shape as ``touch_api_key``.
    """

    def admit(self, workspace_id: str = WORKSPACE) -> None:
        self.store.admit_workspace(
            TENANT,
            workspace_id,
            principal_kind="user",
            principal_id="alice",
            session_key="session-1",
            limit=None,
        )

    def backdate_workspace(self, workspace_id: str, seconds: int) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE sandbox_workspaces SET last_used_at = "
                "datetime('now', ?) WHERE workspace_id = ?",
                (f"-{seconds} seconds", workspace_id),
            )
            connection.commit()

    def recorded_age(self, workspace_id: str) -> int:
        """Seconds since the stored last_used_at, read back through list_workspaces."""
        rows = {
            row["workspace_id"]: row["last_used_at"]
            for row in self.store.list_workspaces(TENANT)
        }
        return int(time.time()) - int(rows[workspace_id])

    def test_a_touched_workspace_leaves_the_idle_candidates(self) -> None:
        self.admit()
        self.backdate_workspace(WORKSPACE, 7200)
        self.assertEqual(self.store.idle_workspaces(3600), [WORKSPACE])
        self.store.touch_workspace(WORKSPACE)
        self.assertEqual(self.store.idle_workspaces(3600), [])
        self.assertLessEqual(self.recorded_age(WORKSPACE), 5)

    def test_a_touch_inside_the_throttle_window_writes_nothing(self) -> None:
        self.admit()
        self.backdate_workspace(WORKSPACE, 100)
        self.store.touch_workspace(WORKSPACE)
        # Still about 100s old: the WHERE refused the write. A touch that always
        # wrote would make the column a constant now() under a polling client.
        self.assertGreaterEqual(self.recorded_age(WORKSPACE), 95)

    def test_a_touch_does_not_revive_a_reclaimed_workspace(self) -> None:
        self.admit()
        self.store.forget_workspace(TENANT, WORKSPACE)
        self.backdate_workspace(WORKSPACE, 7200)
        self.store.touch_workspace(WORKSPACE)
        with sqlite3.connect(self.path) as connection:
            deleted_at, last_used_at = connection.execute(
                "SELECT deleted_at, last_used_at FROM sandbox_workspaces "
                "WHERE workspace_id = ?",
                (WORKSPACE,),
            ).fetchone()
        self.assertIsNotNone(deleted_at)
        recorded = int(store_module._epoch_seconds(last_used_at))
        self.assertGreaterEqual(int(time.time()) - recorded, 7000)

    def test_list_workspaces_reports_the_store_clock_as_epoch_seconds(self) -> None:
        self.admit()
        self.backdate_workspace(WORKSPACE, 600)
        row = self.store.list_workspaces(None)[0]
        self.assertTrue(row["last_used_at"].isdigit(), row)
        self.assertAlmostEqual(int(row["last_used_at"]), int(time.time()) - 600, delta=5)


if __name__ == "__main__":
    unittest.main()
