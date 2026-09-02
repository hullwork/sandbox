"""Where a capability epoch comes from, and what moves it.

The ticket algebra itself is covered by test_capability_tickets. These are the
control-plane halves of the same property: the epoch a ticket is derived under
lives in the row for that sandbox or workspace, so provisioning rotates it,
deleting revokes it, and a subject with no row gets no ticket at all.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_store_behavior import (  # noqa: E402
    TEMPLATE,
    TENANT,
    WORKSPACE,
    StoreCase,
)


class EpochStorageTests(StoreCase):
    def workspace_row(self, workspace_id: str = WORKSPACE) -> None:
        self.store.register_workspace(
            TENANT,
            workspace_id,
            principal_kind="service",
            principal_id="default",
            session_key="session",
        )

    def test_a_new_row_starts_at_a_usable_epoch(self) -> None:
        self.workspace_row()
        self.assertTrue(self.admit("sb-00000000000a"))
        self.assertEqual(self.store.workspace_epoch(WORKSPACE), 1)
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 1)

    def test_an_unknown_subject_has_no_epoch(self) -> None:
        # Fail closed: no row means no ticket can be minted for that id.
        self.assertIsNone(self.store.runtime_epoch("sb-00000000000f"))
        self.assertIsNone(self.store.workspace_epoch("ws-ffffffffffff"))

    def test_a_released_runtime_stops_answering(self) -> None:
        self.assertTrue(self.admit("sb-00000000000a"))
        self.store.release_runtime(TENANT, "sb-00000000000a")
        self.assertIsNone(self.store.runtime_epoch("sb-00000000000a"))

    def test_bumping_returns_the_new_epoch_and_is_repeatable(self) -> None:
        self.workspace_row()
        self.assertTrue(self.admit("sb-00000000000a"))
        self.assertEqual(self.store.bump_runtime_epoch("sb-00000000000a"), 2)
        self.assertEqual(self.store.bump_runtime_epoch("sb-00000000000a"), 3)
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 3)
        self.assertEqual(self.store.bump_workspace_epoch(WORKSPACE), 2)
        self.assertEqual(self.store.workspace_epoch(WORKSPACE), 2)

    def test_bumping_an_unknown_subject_reports_it(self) -> None:
        self.assertIsNone(self.store.bump_runtime_epoch("sb-00000000000f"))
        self.assertIsNone(self.store.bump_workspace_epoch("ws-ffffffffffff"))

    def test_epochs_of_two_sandboxes_move_independently(self) -> None:
        self.assertTrue(self.admit("sb-00000000000a"))
        self.assertTrue(self.admit("sb-00000000000b"))
        self.store.bump_runtime_epoch("sb-00000000000a")
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 2)
        self.assertEqual(self.store.runtime_epoch("sb-00000000000b"), 1)

    def test_a_runtime_row_keeps_its_own_template_and_epoch(self) -> None:
        self.assertTrue(self.admit("sb-00000000000a"))
        row = self.store.get_runtime("sb-00000000000a")
        assert row is not None
        self.assertEqual(row["template"], TEMPLATE)
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 1)


if __name__ == "__main__":
    unittest.main()
