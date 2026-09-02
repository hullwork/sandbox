"""Probe semantics of the Control Plane HTTP adapter during shutdown (control_plane/api.py).

``/readyz`` must flip to 503 as soon as ``control_plane._SHUTTING_DOWN`` is set so
the replica leaves the Service endpoints, while ``/livez`` keeps answering
200 so the kubelet does not restart a process that is draining on purpose.

``control_plane`` reads its environment and opens clients at import, so the server
is started in a subprocess with the volume-role environment (the same shape
as ``tests/test_openapi_contract.py``); the probe prints one JSON document
with every observation and the test asserts on it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from control_plane import api
    from control_plane import core as control_plane
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def probe(path):
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}", method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    observations = {}
    try:
        observations["before"] = {p: probe(p) for p in ("/readyz", "/livez")}
        class ReadyStore:
            def check_ready(self):
                pass
        class UnavailableStore:
            def check_ready(self):
                from control_plane.store import StoreError
                raise StoreError("database is offline")
        control_plane.configured_runtime_driver = lambda: type(
            "ReadyDriver", (), {"list_runtimes": lambda self: []}
        )()
        control_plane.OBJECT_STORE_HEALTH_PATH = ""
        control_plane.STORE = ReadyStore()
        observations["database_ready"] = {
            "readyz": probe("/readyz"), "healthz": probe("/healthz")
        }
        control_plane.STORE = UnavailableStore()
        observations["database_down"] = {
            "readyz": probe("/readyz"), "healthz": probe("/healthz")
        }
        control_plane.STORE = None
        control_plane._SHUTTING_DOWN.set()
        observations["during"] = {p: probe(p) for p in ("/readyz", "/livez", "/healthz")}
        observations["during_again"] = {p: probe(p) for p in ("/readyz", "/livez")}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    print(json.dumps(observations))
    """
)


def run_probe() -> dict:
    environment = {
        **os.environ,
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
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ReadinessProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.observations = run_probe()

    def test_probes_are_green_before_shutdown(self) -> None:
        before = self.observations["before"]
        self.assertEqual(before["/readyz"], [200, {"status": "ready"}])
        self.assertEqual(before["/livez"], [200, {"status": "alive"}])

    def test_database_is_reported_without_removing_the_only_ready_endpoint(self) -> None:
        self.assertEqual(
            self.observations["database_ready"]["readyz"],
            [200, {"status": "ready"}],
        )
        self.assertEqual(
            self.observations["database_down"]["readyz"],
            [200, {"status": "ready"}],
        )
        self.assertEqual(
            self.observations["database_ready"]["healthz"],
            [200, {
                "status": "ok", "database": "ok", "kubernetes": "ok",
                "object_storage": "unchecked",
            }],
        )
        self.assertEqual(
            self.observations["database_down"]["healthz"],
            [503, {"error": "database unavailable", "diagnosis": "database"}],
        )

    def test_readyz_turns_503_once_shutdown_starts(self) -> None:
        during = self.observations["during"]
        self.assertEqual(during["/readyz"], [503, {"status": "shutting down"}])
        # The flag is sticky: the replica never comes back as ready.
        self.assertEqual(
            self.observations["during_again"]["/readyz"],
            [503, {"status": "shutting down"}],
        )

    def test_livez_stays_200_during_shutdown(self) -> None:
        self.assertEqual(self.observations["during"]["/livez"], [200, {"status": "alive"}])
        self.assertEqual(
            self.observations["during_again"]["/livez"], [200, {"status": "alive"}]
        )

    def test_healthz_reports_the_drain_too(self) -> None:
        self.assertEqual(
            self.observations["during"]["/healthz"], [503, {"status": "shutting down"}]
        )


if __name__ == "__main__":
    unittest.main()
