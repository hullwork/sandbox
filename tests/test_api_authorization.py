"""Tenant ownership checks exercised against a SQLite-backed Control Plane.

``test_openapi_contract`` starts the Control Plane without a store, and
``require_workspace_tenant``/``require_sandbox_tenant`` are unconditionally
True when ``STORE is None``: the ownership logic never ran in CI. This test
boots ``api.ApiHandler`` in a subprocess with ``SANDBOX_STORE_BACKEND=sqlite``,
creates two tenants through the admin routes, lets tenant A create a workspace
and a (pending) Runtime, then replays every by-id route with tenant B's key.

Kubernetes and the volume agent are replaced in-process by recording fakes:
the Kubernetes fake fails every mutating call with 503, so a request that is
rejected with 404 *before* any Kubernetes or volume call proves that ownership
is checked ahead of the downstream dependency, while tenant A reaching the
503 proves the same request passes the ownership gate.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_response_schemas import load_spec, validate  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json
    import sys
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from control_plane import kube
    class FakeKube:
        calls = []

        def __init__(self):
            pass

        def _fail(self, name):
            FakeKube.calls.append(name)
            raise kube.KubeError(503, "kubernetes unavailable in this test")

        def list(self, namespace, plural, *, label_selector=None):
            FakeKube.calls.append("list")
            return []

        def get(self, namespace, plural, name):
            return self._fail("get")

        def patch_annotations(self, namespace, plural, name, annotations):
            return self._fail("patch")

        def create_or_get(self, namespace, plural, name, manifest):
            return self._fail("create_or_get")

        def delete(self, namespace, plural, name):
            return self._fail("delete")

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane
    VOLUME = {"calls": [], "workspaces": {}}

    def fake_volume(method, path, payload=None, query=None, timeout=40):
        VOLUME["calls"].append((method, path))
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/v1/workspaces":
            body = {"workspaces": [
                {"id": key, **value} for key, value in VOLUME["workspaces"].items()
            ]}
            return 200, json.dumps(body).encode(), "application/json"
        workspace_id = parts[2]
        if method == "POST" and len(parts) == 3:
            VOLUME["workspaces"].setdefault(
                workspace_id, {"created_at": "1700000000", "last_used_at": "1700000600"}
            )
            return 200, b'{"created": true}', "application/json"
        if workspace_id not in VOLUME["workspaces"]:
            return 404, b'{"error": "workspace not found"}', "application/json"
        if method == "DELETE":
            del VOLUME["workspaces"][workspace_id]
            return 200, b'{"removed": true}', "application/json"
        return 200, b'{"entries": []}', "application/json"

    control_plane.volume_agent_request = fake_volume
    control_plane.STORE.ensure_schema()

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, method, path, token, body=None):
        before = (len(FakeKube.calls), len(VOLUME["calls"]))
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
        except (urllib.error.URLError, ConnectionError) as exc:
            status, raw = None, json.dumps({"transport_error": str(exc)}).encode()
        results[name] = {
            "status": status,
            "body": json.loads(raw) if raw else None,
            "kube_calls": len(FakeKube.calls) - before[0],
            "volume_calls": len(VOLUME["calls"]) - before[1],
        }
        return results[name]["body"]

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
        call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
        call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
        key_a = call("key_a", "POST", "/v1/admin/tenants/tenant-a/keys", admin, {"label": "a"})["api_key"]
        key_b = call("key_b", "POST", "/v1/admin/tenants/tenant-b/keys", admin, {"label": "b"})["api_key"]

        lease = call("a_create_workspace", "POST", "/v1/workspaces", key_a, {"session_id": "session-a"})
        ws_a = lease["workspace_id"]
        sb_a = "sb-0123456789ab"
        results["preseed_runtime"] = control_plane.STORE.admit_runtime("tenant-a", sb_a, ws_a, "default", 5)
        results["ws_a"] = ws_a

        call("a_whoami", "GET", "/v1/whoami", key_a)
        call("a_list_workspaces", "GET", "/v1/workspaces", key_a)
        call("b_list_workspaces", "GET", "/v1/workspaces", key_b)
        call("b_resolve", "POST", "/v1/workspaces/resolve", key_b, {"session_id": "session-a"})

        call("b_create_sandbox", "POST", "/v1/sandboxes", key_b, {"workspace_id": ws_a})
        call("b_list_checkpoints", "GET", f"/v1/workspaces/{ws_a}/checkpoints", key_b)
        call("b_create_checkpoint", "POST", f"/v1/workspaces/{ws_a}/checkpoints", key_b, {})
        call("b_files_list", "GET", f"/v1/workspaces/{ws_a}/files/list?path=.", key_b)
        call("b_get_sandbox", "GET", f"/v1/sandboxes/{sb_a}", key_b)
        call("b_sandbox_token", "POST", f"/v1/sandboxes/{sb_a}/token", key_b, {})
        call("b_delete_sandbox", "DELETE", f"/v1/sandboxes/{sb_a}", key_b)
        call("b_delete_workspace", "DELETE", f"/v1/workspaces/{ws_a}", key_b)

        call("a_get_sandbox", "GET", f"/v1/sandboxes/{sb_a}", key_a)
        call("a_sandbox_token", "POST", f"/v1/sandboxes/{sb_a}/token", key_a, {})
        call("a_files_list", "GET", f"/v1/workspaces/{ws_a}/files/list?path=.", key_a)
        call("a_create_sandbox", "POST", "/v1/sandboxes", key_a, {"workspace_id": ws_a})
        call("a_delete_workspace", "DELETE", f"/v1/workspaces/{ws_a}", key_a)
        results["workspace_on_volume_after_delete"] = ws_a in VOLUME["workspaces"]
        call("admin_audit", "GET", "/v1/admin/audit?limit=50", admin)
        results["audit_denied"] = [
            (event["action"], event["target"])
            for event in control_plane.STORE.list_audit(limit=200)
            if event.get("outcome") == "denied"
        ]
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)


def run_probe() -> dict:
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
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class TenantOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()
        cls.results = run_probe()

    def response(self, name: str) -> dict:
        return self.results[name]

    def assert_schema(self, schema: str, value: object) -> None:
        errors = validate(self.spec, {"$ref": f"#/components/schemas/{schema}"}, value)
        self.assertEqual(errors, [], f"{schema}: {value!r}")

    def assert_denied_before_downstream(self, name: str) -> None:
        response = self.response(name)
        self.assertEqual(response["status"], 404, response)
        self.assertEqual(response["body"], {"error": self.expected_message(name)}, name)
        self.assertEqual(
            (response["kube_calls"], response["volume_calls"]), (0, 0),
            f"{name}: downstream dependency was touched before the ownership gate",
        )

    @staticmethod
    def expected_message(name: str) -> str:
        return "sandbox not found" if "sandbox" in name and "create" not in name else "workspace not found"

    def test_fixture_setup_succeeded(self) -> None:
        self.assertEqual(self.response("tenant_a")["status"], 201)
        self.assert_schema("Tenant", self.response("tenant_a")["body"])
        self.assertEqual(self.response("key_b")["status"], 201)
        self.assert_schema("IssuedApiKey", self.response("key_b")["body"])
        lease = self.response("a_create_workspace")
        self.assertEqual(lease["status"], 201, lease)
        self.assert_schema("WorkspaceLease", lease["body"])
        self.assertEqual(
            lease["volume_calls"], 1,
            "workspace admission must remain one atomic volume-role request",
        )
        self.assertTrue(self.results["preseed_runtime"], "admit_runtime must occupy a slot")

    def test_tenant_b_cannot_act_on_tenant_a_workspace_or_runtime(self) -> None:
        for name in (
            "b_create_sandbox",
            "b_list_checkpoints",
            "b_create_checkpoint",
            "b_files_list",
            "b_get_sandbox",
            "b_sandbox_token",
            "b_delete_sandbox",
            "b_delete_workspace",
        ):
            with self.subTest(route=name):
                self.assert_denied_before_downstream(name)
        self.assertEqual(self.response("b_resolve")["status"], 404)
        self.assertTrue(self.results["workspace_on_volume_after_delete"] is False)

    def test_tenant_a_workspace_is_invisible_to_tenant_b(self) -> None:
        listed_by_a = [item["id"] for item in self.response("a_list_workspaces")["body"]["workspaces"]]
        listed_by_b = [item["id"] for item in self.response("b_list_workspaces")["body"]["workspaces"]]
        self.assertIn(self.results["ws_a"], listed_by_a)
        self.assertNotIn(self.results["ws_a"], listed_by_b)

    def test_denials_are_audited(self) -> None:
        listing = self.response("admin_audit")
        self.assertEqual(listing["status"], 200, listing)
        self.assertTrue(listing["body"]["events"], "admin audit listing must not be empty")
        denied = {tuple(item) for item in self.results["audit_denied"]}
        self.assertIn(("workspace.access", self.results["ws_a"]), denied)
        self.assertIn(("sandbox.access", "sb-0123456789ab"), denied)

    def test_tenant_a_passes_the_ownership_gate(self) -> None:
        pending = self.response("a_get_sandbox")
        self.assertEqual(pending["status"], 200, pending)
        self.assertEqual(pending["body"]["status"], "pending")
        self.assert_schema("Sandbox", pending["body"])
        # The token route reaches Kubernetes only after the gate; the fake
        # answers 503, which is the proof that A was not stopped by ownership.
        token = self.response("a_sandbox_token")
        self.assertEqual((token["status"], token["kube_calls"]), (503, 1), token)
        files = self.response("a_files_list")
        self.assertEqual(files["status"], 409, files)
        self.assertEqual(files["kube_calls"], 1, files)
        create = self.response("a_create_sandbox")
        self.assertNotEqual(create["status"], 404, create)
        self.assertGreaterEqual(create["volume_calls"], 1, create)
        deleted = self.response("a_delete_workspace")
        self.assertEqual(deleted["status"], 200, deleted)
        self.assert_schema("WorkspaceDeleted", deleted["body"])
        self.assertFalse(self.results["workspace_on_volume_after_delete"])

    def test_live_responses_match_openapi_schemas(self) -> None:
        self.assert_schema("Identity", self.response("a_whoami")["body"])
        for item in self.response("a_list_workspaces")["body"]["workspaces"]:
            self.assert_schema("Workspace", item)
        self.assert_schema("Error", self.response("b_get_sandbox")["body"])
        self.assert_schema("Error", self.response("a_sandbox_token")["body"])
        self.assert_schema("Error", self.response("a_files_list")["body"])


if __name__ == "__main__":
    unittest.main()
