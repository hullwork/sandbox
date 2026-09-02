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
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


#: Keyed by the status each sample exercises, so a status that gains no sample
#: fails test_every_outage_status_has_a_sample below.
OUTAGE_STATUS_SAMPLES = {
    # "Slow down" is an instruction to wait; the request was fine.
    429: "SlowDown",
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
    # Raised while a body is being consumed rather than while the request is
    # made. A download that dies halfway is the storage going out of reach.
    "ResponseStreamingError": {"error": "connection reset"},
    "IncompleteReadError": {"actual_bytes": 400, "expected_bytes": 1000},
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
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from control_plane import core

TRANSPORT = {
    "EndpointConnectionError": EndpointConnectionError,
    "ConnectTimeoutError": ConnectTimeoutError,
    "ReadTimeoutError": ReadTimeoutError,
    "ConnectionClosedError": ConnectionClosedError,
    "ResponseStreamingError": ResponseStreamingError,
    "IncompleteReadError": IncompleteReadError,
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
# Some stores spell throttling as 503 + SlowDown, others as 400 + SlowDown;
# the code alone must be enough.
results["slowdown-by-code"] = core.failure_is_outage(client_error(400, "SlowDown"))

# What the caller actually receives, by rejection kind.
def translated(status, code):
    error = core._translate(client_error(status, code))
    return {
        "types": [cls.__name__ for cls in type(error).__mro__ if cls is not object],
        "message": str(error),
    }
results["translate-404"] = translated(404, "NoSuchKey")
results["translate-403"] = translated(403, "AccessDenied")
results["translate-400"] = translated(400, "InvalidRequest")
results["translate-429"] = translated(429, "SlowDown")

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

    def test_throttling_is_an_outage_by_code_as_well_as_by_status(self) -> None:
        self.assertTrue(self.probe["results"]["slowdown-by-code"])

    def test_each_rejection_kind_gets_its_own_answer(self) -> None:
        """Three sentences for three different things to fix.

        Until 2026-09-02 a missing checkpoint, bad credentials and a malformed
        request all produced "object storage rejected the operation" as a 400,
        so the caller could not tell whether to fix the id, the credentials or
        the request."""
        missing = self.probe["results"]["translate-404"]
        self.assertIn("FileNotFoundError", missing["types"], missing)
        self.assertEqual(missing["message"], "object not found")
        refused = self.probe["results"]["translate-403"]
        self.assertIn("RuntimeError", refused["types"])
        self.assertNotIn("ObjectStoreBusy", refused["types"], "403 is not retryable")
        self.assertIn("credentials", refused["message"])
        rejected = self.probe["results"]["translate-400"]
        self.assertEqual(rejected["message"], "object storage rejected the operation")
        throttled = self.probe["results"]["translate-429"]
        self.assertIn("ObjectStoreUnavailable", throttled["types"])


API_PROBE = textwrap.dedent(
    """
    import json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    from botocore.exceptions import ClientError

    from control_plane import kube

    class FakeKube:
        def __init__(self):
            pass
        def list(self, namespace, plural, *, label_selector=None):
            return []

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane

    STATUS = {"value": 404, "code": "NoSuchKey"}

    class FakeStore:
        def get_object(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": STATUS["code"], "Message": "store text, never surfaced"},
                 "ResponseMetadata": {"HTTPStatusCode": STATUS["value"]}},
                "GetObject",
            )
        head_object = get_object
        delete_object = get_object

    control_plane.object_store = lambda: FakeStore()
    control_plane.STORE.ensure_schema()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    results = {}

    def get(name, status, code):
        STATUS["value"], STATUS["code"] = status, code
        query = urllib.parse.urlencode({"scope": "agent", "agent_id": "agent-1", "run_id": "run-1",
                                        "owner": "acme/alice", "path": "outputs/report.txt"})
        request = urllib.request.Request(f"{base}/v1/storage/objects?{query}", method="GET",
                                         headers={"Authorization": f"Bearer {admin}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                results[name] = {"status": response.status, "body": json.loads(response.read())}
        except urllib.error.HTTPError as exc:
            results[name] = {"status": exc.code, "body": json.loads(exc.read())}

    try:
        get("missing", 404, "NoSuchKey")
        get("forbidden", 403, "AccessDenied")
        get("throttled", 429, "SlowDown")
        get("malformed", 400, "InvalidRequest")
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)


def run_api_probe() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-admin-token",
            "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED": "true",
            "SIGNING_KEY": "0" * 32,
            "WORKSPACE_ID_KEY": "1" * 32,
            "SANDBOX_STORE_BACKEND": "sqlite",
            "SANDBOX_STORE_PATH": os.path.join(directory, "control-plane.db"),
            "VOLUME_AGENT_URL": "http://127.0.0.1:1",
            "VOLUME_AGENT_TOKEN": "test-volume-token",
            "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
            "OBJECT_STORE_ACCESS_KEY": "test-access",
            "OBJECT_STORE_SECRET_KEY": "test-secret",
            "PYTHONPATH": str(ROOT),
        }
        environment.pop("SANDBOX_CONTROL_PLANE_ROLE", None)
        for name in list(environment):
            if name.startswith("SANDBOX_CONTROL_PLANE_OIDC_"):
                environment.pop(name)
        result = subprocess.run(
            [sys.executable, "-c", API_PROBE],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ApiStatusMappingTests(unittest.TestCase):
    """What a caller of GET /v1/storage/objects sees for each store answer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_api_probe()

    def test_a_missing_object_is_404(self) -> None:
        self.assertEqual(self.results["missing"], {"status": 404, "body": {"error": "object not found"}})

    def test_refused_access_is_400_and_names_the_credentials(self) -> None:
        refused = self.results["forbidden"]
        self.assertEqual(refused["status"], 400, refused)
        self.assertIn("credentials", refused["body"]["error"])
        self.assertNotIn("store text", refused["body"]["error"], "the store's own words must not surface")

    def test_throttling_is_503_with_a_retry_hint(self) -> None:
        throttled = self.results["throttled"]
        self.assertEqual(throttled["status"], 503, throttled)
        self.assertIn("retry_after_seconds", throttled["body"])

    def test_a_malformed_request_stays_400(self) -> None:
        self.assertEqual(self.results["malformed"]["status"], 400)


if __name__ == "__main__":
    unittest.main()
