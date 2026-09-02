"""Classification of a failed mc invocation: storage out of reach, or request rejected.

``control_plane.core.mc_failure_is_outage`` decides that from stderr alone, and
the two misjudgments cost differently - calling a missing object an outage only
wastes a retry, while calling an unreachable endpoint a rejection sends the
caller off to rotate credentials when it should have waited. The function had no
test, so a marker could be reworded into one that never fires and every suite
would stay green.

``core.py`` reads its environment on import, so the classification runs once in a
subprocess under a minimal volume-role environment and the table is asserted here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Keyed by the marker each sample is meant to exercise, so a marker that gains no
# sample fails test_every_declared_marker_has_a_sample below.
OUTAGE_SAMPLES = {
    "connection refused": (
        b'mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Get "http://store:9000/ws-1/a.txt": '
        b"dial tcp 10.0.0.5:9000: connect: connection refused."
    ),
    "no such host": (
        b'mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Get "http://store:9000/ws-1/a.txt": '
        b"dial tcp: lookup store on 10.96.0.10:53: no such host."
    ),
    "i/o timeout": (
        b'mc: <ERROR> Unable to write `store/ws-1/a.txt`. Put "http://store:9000/ws-1/a.txt": '
        b"dial tcp 10.0.0.5:9000: i/o timeout."
    ),
    "context deadline exceeded": (
        b'mc: <ERROR> Unable to list `store/ws-1`. Get "http://store:9000/ws-1": '
        b"context deadline exceeded (Client.Timeout exceeded while awaiting headers)."
    ),
    "connection reset by peer": (
        b"mc: <ERROR> Unable to write `store/ws-1/a.txt`. read tcp 10.0.0.9:41234->10.0.0.5:9000: "
        b"read: connection reset by peer."
    ),
    "network is unreachable": (
        b'mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Get "http://store:9000/ws-1/a.txt": '
        b"dial tcp 10.0.0.5:9000: connect: network is unreachable."
    ),
    "no route to host": (
        b'mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Get "http://store:9000/ws-1/a.txt": '
        b"dial tcp 10.0.0.5:9000: connect: no route to host."
    ),
    # MinIO Server's phrasing; this sample proves the marker fires, not that the
    # deployed store can produce it. See the note beside the marker in core.py.
    "server not initialized": (
        b"mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Server not initialized, please try again."
    ),
    "unable to initialize new alias": (
        b"mc: <ERROR> Unable to initialize new alias from the provided credentials."
    ),
}

REJECTION_SAMPLES = {
    "missing object": b"mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Object does not exist.",
    "missing bucket": b"mc: <ERROR> Unable to stat `store/absent`. Bucket does not exist.",
    "missing key": b"mc: <ERROR> Unable to stat `store/ws-1/a.txt`. The specified key does not exist.",
    "denied": b"mc: <ERROR> Unable to copy `store/ws-1/a.txt`. Access Denied.",
    "bad prefix": b"mc: <ERROR> Unable to list `store/ws-1/nope`. Prefix does not exist.",
    # A non-zero exit that said nothing must not be promoted to an outage: an
    # empty marker set means "unclassified", and unclassified is a rejection.
    "silent failure": b"",
}

CASE_SAMPLES = {
    "shouting": b"mc: <ERROR> Unable to stat `store/ws-1/a.txt`. CONNECTION REFUSED.",
    "mixed case": b"mc: <ERROR> Unable to stat `store/ws-1/a.txt`. Connection Refused.",
}

# stderr is external text; mc has no obligation to keep it valid UTF-8.
UNDECODABLE_SAMPLES = {
    "invalid utf-8 outage": b"mc: <ERROR> \xff\xfe connect: connection refused.",
    "invalid utf-8 rejection": b"mc: <ERROR> \xff\xfe Access Denied.",
}

PROBE = """
import json
from control_plane import core

print(json.dumps({
    "markers": list(core._MC_OUTAGE_MARKERS),
    "results": {name: core.mc_failure_is_outage(value) for name, value in SAMPLES.items()},
}))
"""


def run_probe(samples: dict[str, bytes]) -> dict:
    environment = {
        **os.environ,
        # The volume role skips the Kubernetes client, which core.py builds at
        # import time and which needs an in-cluster service account.
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
        "SIGNING_KEY": "0" * 32,
        "WORKSPACE_ID_KEY": "1" * 32,
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", f"SAMPLES = {samples!r}\n{PROBE}"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ObjectStoreFailureClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = run_probe(
            {
                **OUTAGE_SAMPLES,
                **REJECTION_SAMPLES,
                **CASE_SAMPLES,
                **UNDECODABLE_SAMPLES,
            }
        )

    def test_every_declared_marker_has_a_sample(self) -> None:
        # A marker nobody ever fed a matching string to is indistinguishable from
        # a marker that cannot match anything.
        self.assertEqual(sorted(self.probe["markers"]), sorted(OUTAGE_SAMPLES))

    def test_unreachable_storage_is_an_outage(self) -> None:
        for name in OUTAGE_SAMPLES:
            with self.subTest(marker=name):
                self.assertTrue(self.probe["results"][name])

    def test_refused_requests_are_not_an_outage(self) -> None:
        for name in REJECTION_SAMPLES:
            with self.subTest(sample=name):
                self.assertFalse(self.probe["results"][name])

    def test_marker_matching_ignores_case(self) -> None:
        for name in CASE_SAMPLES:
            with self.subTest(sample=name):
                self.assertTrue(self.probe["results"][name])

    def test_undecodable_stderr_is_still_classified(self) -> None:
        self.assertTrue(self.probe["results"]["invalid utf-8 outage"])
        self.assertFalse(self.probe["results"]["invalid utf-8 rejection"])


if __name__ == "__main__":
    unittest.main()
