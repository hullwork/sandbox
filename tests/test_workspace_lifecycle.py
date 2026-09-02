"""The Workspace idle clock: who refreshes it, and which clock the view shows.

The reaper's verdict is ``sandbox_workspaces.last_used_at`` and nothing else.
Until 2026-09-02 the only writer of that column was workspace admission, so a
client that took its lease once and then only ever talked to its Runtime
(files, objects, checkpoints, MCP) never refreshed it, and the round that
deleted its Runtime on the hard TTL also swept the Workspace. Three things pin
the fix:

* the data-plane routes touch the store column **after** the ownership gate,
  so an unowned id refreshes nobody's clock (a live Control Plane on SQLite);
* ``workspace_view`` derives ``idle_expires_at`` from the store clock when it
  is given one, so the countdown shown is the countdown the reaper runs;
* the TTL relationship that made this fatal is reported at startup.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_response_schemas import load_view_functions  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_ttl_advisory():
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "workspace_ttl_advisory"
    ]
    assert len(body) == 1, "core.py must define workspace_ttl_advisory once"
    namespace: dict = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "core.py", "exec"), namespace)
    return namespace["workspace_ttl_advisory"]


class WorkspaceViewClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.views = load_view_functions()

    def view(self, *args, **kwargs) -> dict:
        return self.views["workspace_view"](*args, **kwargs)

    def test_the_store_clock_drives_the_countdown_when_supplied(self) -> None:
        entry = {"id": "ws-0123456789ab", "created_at": "1700000000", "last_used_at": "1700000600"}
        shown = self.view(entry, False, recorded_last_used_at="1700009000")
        # 21600 is the TTL the loader supplies; the marker (1700000600) is still
        # reported as last_used_at, but the countdown follows the store column.
        self.assertEqual(shown["idle_expires_at"], str(1700009000 + 21600))
        self.assertEqual(shown["last_used_at"], "1700000600")
        self.assertNotIn("recorded_last_used_at", shown, "the schema forbids extra fields")

    def test_without_a_store_clock_the_marker_still_counts(self) -> None:
        entry = {"id": "ws-0123456789ab", "created_at": "1700000000", "last_used_at": "1700000600"}
        self.assertEqual(self.view(entry, False)["idle_expires_at"], str(1700000600 + 21600))
        self.assertIsNone(self.view(entry, True, recorded_last_used_at="1700009000")["idle_expires_at"])


class TtlAdvisoryTests(unittest.TestCase):
    def test_idle_ttl_not_above_hard_ttl_is_reported_not_refused(self) -> None:
        advisory = load_ttl_advisory()
        message = advisory(21600, 43200)
        self.assertIsInstance(message, str)
        self.assertIn("WORKSPACE_IDLE_TTL_SECONDS=21600", message)
        self.assertIn("SANDBOX_RUNTIME_HARD_TTL_SECONDS=43200", message)
        self.assertIsNotNone(advisory(43200, 43200), "equal is still not above")
        self.assertIsNone(advisory(43201, 43200))


PROBE = textwrap.dedent(
    """
    import json
    import sqlite3
    import threading
    import time
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from control_plane import kube

    class FakeKube:
        def __init__(self):
            pass
        def list(self, namespace, plural, *, label_selector=None):
            return []
        def get(self, namespace, plural, name):
            raise kube.KubeError(503, "kubernetes unavailable in this test")
        def patch_annotations(self, namespace, plural, name, annotations):
            raise kube.KubeError(503, "kubernetes unavailable in this test")
        def create_or_get(self, namespace, plural, name, manifest):
            raise kube.KubeError(503, "kubernetes unavailable in this test")
        def delete(self, namespace, plural, name):
            raise kube.KubeError(503, "kubernetes unavailable in this test")

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane

    VOLUME = {}

    def fake_volume(method, path, payload=None, query=None, timeout=40):
        if method == "GET" and path == "/v1/workspaces":
            body = {"workspaces": [{"id": key, **value} for key, value in VOLUME.items()]}
            return 200, json.dumps(body).encode(), "application/json"
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 3:
            # No last_used_at marker on the volume: the store column is the only clock.
            VOLUME.setdefault(parts[2], {"created_at": "1700000000", "last_used_at": None})
            return 200, b'{"created": true}', "application/json"
        return 200, b'{"entries": []}', "application/json"

    control_plane.volume_agent_request = fake_volume
    control_plane.STORE.ensure_schema()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, method, path, token, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{base}{path}", method=method, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        results[name] = {"status": status, "body": json.loads(raw) if raw else None}
        return results[name]["body"]

    def backdate(workspace_id, seconds):
        with sqlite3.connect(STORE_PATH) as connection:
            connection.execute(
                "UPDATE sandbox_workspaces SET last_used_at = datetime('now', ?) "
                "WHERE workspace_id = ?", (f"-{seconds} seconds", workspace_id))
            connection.commit()

    def age(workspace_id):
        rows = {r["workspace_id"]: r["last_used_at"] for r in control_plane.STORE.list_workspaces(None)}
        return int(time.time()) - int(rows[workspace_id])

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
        call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
        call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
        key_a = call("key_a", "POST", "/v1/admin/tenants/tenant-a/keys", admin, {"label": "a"})["api_key"]
        key_b = call("key_b", "POST", "/v1/admin/tenants/tenant-b/keys", admin, {"label": "b"})["api_key"]
        lease = call("lease", "POST", "/v1/workspaces", key_a, {"session_id": "session-a"})
        ws = lease["workspace_id"]
        scoped = lease["access_token"]

        backdate(ws, 7200)
        results["age_before"] = age(ws)
        # Denied by ownership: must not move the clock.
        call("b_files", "GET", f"/v1/workspaces/{ws}/files/list?path=.", key_b)
        results["age_after_denied"] = age(ws)
        # Owner, read subset (409: no Runtime) - the gate passed, so the touch lands.
        call("a_files", "GET", f"/v1/workspaces/{ws}/files/list?path=.", key_a)
        results["age_after_owner_read"] = age(ws)

        backdate(ws, 7200)
        call("a_checkpoints", "GET", f"/v1/workspaces/{ws}/checkpoints", key_a)
        results["age_after_checkpoint_list"] = age(ws)

        backdate(ws, 7200)
        # Scoped token, write subset: 409 without a Runtime, but the touch is before the proxy.
        call("scoped_write", "POST", f"/v1/workspaces/{ws}/files/write", scoped, {"path": "a.txt", "content": "x"})
        results["age_after_scoped_write"] = age(ws)

        backdate(ws, 7200)
        # The listing shows the store clock: idle_expires_at must follow the fresh column, not the
        # 1700000600 marker the volume fake would report.
        listing = call("a_list", "GET", "/v1/workspaces", key_a)
        results["listing_status"] = results["a_list"]["status"]
        results["ws"] = ws
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)


def run_probe() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        store_path = os.path.join(directory, "control-plane.db")
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-admin-token",
            "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED": "true",
            "SIGNING_KEY": "0" * 32,
            "WORKSPACE_ID_KEY": "1" * 32,
            "SANDBOX_STORE_BACKEND": "sqlite",
            "SANDBOX_STORE_PATH": store_path,
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
            [sys.executable, "-c", f"STORE_PATH = {store_path!r}\n" + PROBE],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class RouteTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def test_fixture(self) -> None:
        self.assertEqual(self.results["lease"]["status"], 201, self.results["lease"])
        self.assertGreaterEqual(self.results["age_before"], 7000)

    def test_a_denied_request_does_not_refresh_the_clock(self) -> None:
        self.assertEqual(self.results["b_files"]["status"], 404)
        self.assertGreaterEqual(self.results["age_after_denied"], 7000)

    def test_owner_data_plane_requests_refresh_the_clock_after_the_gate(self) -> None:
        for name, response in (
            ("age_after_owner_read", "a_files"),
            ("age_after_checkpoint_list", "a_checkpoints"),
            ("age_after_scoped_write", "scoped_write"),
        ):
            with self.subTest(route=response):
                self.assertNotEqual(self.results[response]["status"], 404, self.results[response])
                self.assertLessEqual(self.results[name], 5, self.results[response])

    def test_the_listing_counts_down_from_the_store_clock(self) -> None:
        self.assertEqual(self.results["listing_status"], 200)
        views = {item["id"]: item for item in self.results["a_list"]["body"]["workspaces"]}
        view = views[self.results["ws"]]
        # The volume fake reports no marker; the store column was backdated by
        # 7200s just before the listing, so the countdown ends 21600-7200 from now.
        import time as _time
        self.assertAlmostEqual(
            int(view["idle_expires_at"]), int(_time.time()) + 21600 - 7200, delta=30,
        )


if __name__ == "__main__":
    unittest.main()
