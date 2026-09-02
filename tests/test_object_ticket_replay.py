"""An object ticket is spent by its first use, across threads and restarts.

``consume_object_ticket`` records the ticket's ``jti`` as a Kubernetes Lease
whose name is derived from it; a second request with the same ticket meets
``AlreadyExists`` (409) from the API server and is refused with 401 before
any object-storage call. The Kubernetes fake here keeps the set of Lease
names it has created and answers 409 for a repeat, which is the whole of
what the real API server contributes to the property.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from control_plane_probe import run_probe  # noqa: E402

PROBE_BODY = """
    LEASES = []

    def create_group(self, namespace, group, version, plural, manifest):
        name = manifest["metadata"]["name"]
        FakeKube.calls.append(f"create_group:{plural}")
        if (namespace, plural, name) in LEASES:
            raise kube.KubeError(409, f"leases.coordination.k8s.io {name} already exists")
        LEASES.append((namespace, plural, name))
        return manifest

    FakeKube.create_group = create_group

    # Object storage is unreachable in this probe, so a request that passes
    # the ticket gate is answered from the object-store failure path, never
    # with a 401; the ticket gate is what these statuses tell apart.
    call("tenant", "POST", "/v1/admin/tenants", admin, {"id": "acme"})
    key = call("key", "POST", "/v1/admin/tenants/acme/keys", admin,
               {"label": "k", "permissions": ["act_as_subjects"]})["body"]["api_key"]
    subject = "b" * 32
    ticket_body = {"operation": "download", "scope": "upload", "upload_id": "u1", "path": "source/a.txt"}
    first = call("ticket_1", "POST", "/v1/storage/tickets", key, ticket_body,
                 headers={"X-Acting-Subject": subject})["body"]
    second = call("ticket_2", "POST", "/v1/storage/tickets", key, ticket_body,
                  headers={"X-Acting-Subject": subject})["body"]
    results["distinct_tickets"] = first["access_token"] != second["access_token"]

    call("use_1", "GET", "/v1/storage/content", first["access_token"])
    results["leases_after_first"] = len(LEASES)
    call("use_1_again", "GET", "/v1/storage/content", first["access_token"])
    results["leases_after_replay"] = len(LEASES)
    call("use_1_third", "GET", "/v1/storage/content", first["access_token"])
    call("use_2", "GET", "/v1/storage/content", second["access_token"])
    results["leases_after_second"] = len(LEASES)
    results["lease_calls"] = [c for c in FakeKube.calls if c.startswith("create_group")]
"""


class ObjectTicketReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results, _ = run_probe(PROBE_BODY)

    def test_fixture_setup_succeeded(self) -> None:
        self.assertEqual(self.results["ticket_1"]["status"], 201, self.results["ticket_1"])
        self.assertEqual(self.results["ticket_2"]["status"], 201, self.results["ticket_2"])
        self.assertTrue(self.results["distinct_tickets"])

    def test_the_first_use_spends_the_ticket(self) -> None:
        first = self.results["use_1"]
        self.assertNotEqual(first["status"], 401, first)
        self.assertEqual(first["kube_calls"], 1, first)
        self.assertEqual(self.results["leases_after_first"], 1)

    def test_a_replay_is_refused_and_creates_nothing(self) -> None:
        for name in ("use_1_again", "use_1_third"):
            with self.subTest(attempt=name):
                replay = self.results[name]
                self.assertEqual(replay["status"], 401, replay)
                self.assertIn("already used", replay["body"]["error"])
                self.assertEqual(replay["kube_calls"], 1, "the Lease create is the replay check")
        self.assertEqual(self.results["leases_after_replay"], 1)

    def test_a_different_ticket_for_the_same_object_is_its_own_lease(self) -> None:
        self.assertNotEqual(self.results["use_2"]["status"], 401, self.results["use_2"])
        self.assertEqual(self.results["leases_after_second"], 2)
        self.assertEqual(len(self.results["lease_calls"]), 4)


if __name__ == "__main__":
    unittest.main()
