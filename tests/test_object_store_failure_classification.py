"""Classification of a failed object-store call: storage out of reach, or request rejected.

``control_plane.core.failure_is_outage`` decides that, and the two misjudgments
cost differently - calling a missing object an outage only wastes a retry, while
calling an unreachable endpoint a rejection sends the caller off to rotate
credentials when it should have waited.

Until 2026-09-02 this read the stderr of an ``mc`` subprocess and matched
substrings, and the note beside the markers admitted the hole: RGW answering 503
while the cluster was degraded produced wording nobody had captured, so a real
outage landed in "rejected" -- the expensive direction. botocore reports the
status code, so a 5xx is now classified from the response. The sample for 503 in
this file is the case the old implementation could not get right.

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


#: Keyed by the status each sample exercises, so a status that gains no sample
#: fails test_every_outage_status_has_a_sample below.
OUTAGE_STATUS_SAMPLES = {
    500: "InternalError",
    502: "BadGateway",
    # The case the substring matcher never handled: Ceph RGW returns this while
    # the cluster is degraded, and the caller has to wait, not re-sign.
    503: "ServiceUnavailable",
    504: "GatewayTimeout",
}

#: Likewise for transport failures, keyed by the botocore exception class name.
OUTAGE_EXCEPTION_SAMPLES = {
    "EndpointConnectionError": {"endpoint_url": "http://127.0.0.1:1"},
    "ConnectTimeoutError": {"endpoint_url": "http://127.0.0.1:1"},
    "ReadTimeoutError": {"endpoint_url": "http://127.0.0.1:1"},
    "ConnectionClosedError": {"endpoint_url": "http://127.0.0.1:1"},
}

#: Answered, and the answer was no. None of these may be promoted to an outage.
REJECTION_STATUS_SAMPLES = {
    400: "InvalidRequest",
    403: "AccessDenied",
    404: "NoSuchKey",
    409: "BucketNotEmpty",
}

PROBE = """
import json
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from control_plane import core

TRANSPORT = {
    "EndpointConnectionError": EndpointConnectionError,
    "ConnectTimeoutError": ConnectTimeoutError,
    "ReadTimeoutError": ReadTimeoutError,
    "ConnectionClosedError": ConnectionClosedError,
}


def client_error(status, code):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "external text, never surfaced"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "GetObject",
    )


results = {}
for status, code in OUTAGE_STATUS.items():
    results[f"status-{status}"] = core.failure_is_outage(client_error(int(status), code))
for status, code in REJECTION_STATUS.items():
    results[f"status-{status}"] = core.failure_is_outage(client_error(int(status), code))
for name, kwargs in TRANSPORT_SAMPLES.items():
    results[name] = core.failure_is_outage(TRANSPORT[name](**kwargs))

# A failure that is neither is unclassified, and unclassified is a rejection: an
# exception nobody recognised must not be promoted into "wait and retry".
results["unrecognised"] = core.failure_is_outage(RuntimeError("something else"))
# A ClientError with no status at all, which is what a malformed or synthetic
# response looks like.
results["no-status"] = core.failure_is_outage(
    ClientError({"Error": {"Code": "Weird"}}, "GetObject")
)

print(json.dumps({
    "statuses": sorted(core._OUTAGE_STATUS),
    "exceptions": [item.__name__ for item in core._OUTAGE_EXCEPTIONS],
    "results": results,
}))
"""


def run_probe() -> dict:
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
    preamble = (
        f"OUTAGE_STATUS = {OUTAGE_STATUS_SAMPLES!r}\n"
        f"REJECTION_STATUS = {REJECTION_STATUS_SAMPLES!r}\n"
        f"TRANSPORT_SAMPLES = {OUTAGE_EXCEPTION_SAMPLES!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", preamble + PROBE],
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
        cls.probe = run_probe()

    def test_every_outage_status_has_a_sample(self) -> None:
        # A status nobody ever fed a matching response to is indistinguishable
        # from a status that cannot match anything.
        self.assertEqual(
            sorted(self.probe["statuses"]), sorted(OUTAGE_STATUS_SAMPLES)
        )

    def test_every_outage_exception_has_a_sample(self) -> None:
        self.assertEqual(
            sorted(self.probe["exceptions"]),
            sorted(OUTAGE_EXCEPTION_SAMPLES),
        )

    def test_unreachable_storage_is_an_outage(self) -> None:
        for status in OUTAGE_STATUS_SAMPLES:
            with self.subTest(status=status):
                self.assertTrue(self.probe["results"][f"status-{status}"])
        for name in OUTAGE_EXCEPTION_SAMPLES:
            with self.subTest(exception=name):
                self.assertTrue(self.probe["results"][name])

    def test_refused_requests_are_not_an_outage(self) -> None:
        for status in REJECTION_STATUS_SAMPLES:
            with self.subTest(status=status):
                self.assertFalse(self.probe["results"][f"status-{status}"])

    def test_an_unclassified_failure_is_a_rejection(self) -> None:
        self.assertFalse(self.probe["results"]["unrecognised"])
        self.assertFalse(self.probe["results"]["no-status"])


if __name__ == "__main__":
    unittest.main()
