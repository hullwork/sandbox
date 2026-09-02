"""Writers and readers of the project's labels and annotations agree on the key.

The gVisor driver writes ``sandbox.hullwork.com/*`` labels and annotations on
Runtime Pods and reads them back; the reaper reads the same prefix on ticket
Leases. The other reaper tests build RuntimeInstance values through their own
fake driver, so a reader that quietly looked up a different key would keep
those tests green while every Runtime looked freshly expired in a cluster.
These tests go through the real readers with the documented keys.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_plane.drivers import GVisorRuntimeDriver  # noqa: E402
from tests.test_reaper_behavior import (  # noqa: E402
    FakeKube,
    build_control_plane,
    load_reaper,
)

PREFIX = "sandbox.hullwork.com/"


class RuntimePodAnnotationTests(unittest.TestCase):
    def test_the_driver_reads_the_documented_keys_back(self) -> None:
        pod = {
            "metadata": {
                "name": "runtime-sb-0123456789ab",
                "labels": {
                    PREFIX + "sandbox-id": "sb-0123456789ab",
                    PREFIX + "workspace-id": "ws-0123456789ab",
                    PREFIX + "template": "playwright",
                    PREFIX + "tenant": "acme",
                },
                "annotations": {
                    PREFIX + "created-at": "1700000000",
                    PREFIX + "expires-at": "1700001800",
                    PREFIX + "hard-expires-at": "1700043200",
                },
            },
            "spec": {"runtimeClassName": "gvisor", "nodeName": "node-a"},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
        instance = GVisorRuntimeDriver._instance(pod)
        self.assertEqual(instance.runtime_id, "sb-0123456789ab")
        self.assertEqual(instance.workspace_id, "ws-0123456789ab")
        self.assertEqual(instance.template_id, "playwright")
        self.assertEqual(instance.tenant_id, "acme")
        self.assertEqual(instance.created_at, "1700000000")
        self.assertEqual(instance.expires_at, 1700001800)
        self.assertEqual(instance.hard_expires_at, 1700043200)


class LeaseKube(FakeKube):
    def __init__(self, leases: list[dict]) -> None:
        super().__init__([])
        self.leases = leases
        self.removed: list[str] = []

    def list_group(self, namespace, group, version, plural, label_selector=None):
        assert (group, version, plural) == ("coordination.k8s.io", "v1", "leases")
        return list(self.leases)

    def delete_group(self, namespace, group, version, plural, name):
        assert plural == "leases"
        self.removed.append(name)


class TicketLeaseReaperTests(unittest.TestCase):
    def test_an_expired_lease_is_removed_and_a_live_one_kept(self) -> None:
        now = 1_700_000_000
        kube = LeaseKube([
            {"metadata": {"name": "ticket-old", "annotations": {PREFIX + "expires-at": str(now - 1)}}},
            {"metadata": {"name": "ticket-live", "annotations": {PREFIX + "expires-at": str(now + 600)}}},
            {"metadata": {"name": "ticket-unannotated", "annotations": {}}},
        ])
        control_plane = build_control_plane(kube, None)
        control_plane.SYSTEM_NAMESPACE = "sandbox-system"
        control_plane.TICKET_LEASE_SELECTOR = PREFIX + "purpose=object-ticket"
        reaper = load_reaper(control_plane)
        removed = reaper.reap_expired_ticket_leases(now=now)
        self.assertEqual(removed, 1)
        self.assertEqual(kube.removed, ["ticket-old"])


if __name__ == "__main__":
    unittest.main()
