"""The reserved tenant name ``management`` cannot be taken by anyone.

The unscoped management identity files its workspaces and runtimes under a
real tenant row called ``management`` (``ApiHandler.ensure_management_tenant``),
so that capability epochs can be revoked for it. A real row means every
ownership check that compares ``owner == self.tenant_id`` would also pass for
a *tenant* credential whose tenant is ``management``. Before this test, an
administrator could ``POST /v1/admin/tenants {"id": "management"}``, issue a
tenant key for it, and that key listed and deleted the management plane's own
workspaces; an OIDC tenant claim of ``management`` did the same through the
browser.

Four entry points name a tenant: tenant creation, key issuance, the tenant a
credential represents (session claim, or ``X-Sandbox-Tenant`` on an admin
key), and the OIDC role mapping. Two more can only hurt it: suspending or
deleting the row through the admin routes would suspend everything the
management plane itself created, with no credential left to undo it. Each is exercised here against the real
handler over HTTP with a SQLite store; the row itself is still created on
demand by the management plane, through the internal entry ``create_tenant``
no longer offers.
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

from control_plane import oidc
from control_plane.store import MANAGEMENT_TENANT

ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import json
    import urllib.error
    import urllib.request
    import threading
    from http.server import ThreadingHTTPServer

    from control_plane import kube
    class FakeKube:
        def __init__(self):
            pass

        def list(self, namespace, plural, *, label_selector=None):
            return []

    kube.KubeClient = FakeKube

    from control_plane import api, session
    from control_plane import core as control_plane
    from control_plane.store import StoreError

    VOLUME = {"workspaces": {}}

    def fake_volume(method, path, payload=None, query=None, timeout=40):
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
        return 404, b'{"error": "workspace not found"}', "application/json"

    control_plane.volume_agent_request = fake_volume
    control_plane.STORE.ensure_schema()

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, method, path, token=None, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{base}{path}", method=method, data=data, headers=request_headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        results[name] = {"status": status, "body": json.loads(raw) if raw else None}
        return results[name]

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
        call("create_reserved", "POST", "/v1/admin/tenants", admin, {"id": "management"})
        results["row_after_create_attempt"] = control_plane.STORE.get_tenant("management") is not None
        call("create_other", "POST", "/v1/admin/tenants", admin, {"id": "managementx"})

        call("admin_as_reserved", "GET", "/v1/whoami", admin, headers={"X-Sandbox-Tenant": "management"})
        call("admin_as_other", "GET", "/v1/whoami", admin, headers={"X-Sandbox-Tenant": "managementx"})

        # The management plane itself still gets its row, on demand.
        call("admin_workspace", "POST", "/v1/workspaces", admin, {"session_id": "ops"})
        results["row_after_admin_workspace"] = control_plane.STORE.get_tenant("management") is not None

        call("key_for_reserved", "POST", "/v1/admin/tenants/management/keys", admin, {"label": "x"})
        call("key_for_other", "POST", "/v1/admin/tenants/managementx/keys", admin, {"label": "x"})
        try:
            control_plane.STORE.issue_api_key("management", "direct")
            results["store_issue"] = "issued"
        except StoreError as exc:
            results["store_issue"] = str(exc)
        try:
            control_plane.STORE.create_tenant("management", "m", max_workspaces=1, max_runtimes=1)
            results["store_create"] = "created"
        except StoreError as exc:
            results["store_create"] = str(exc)

        call("suspend_reserved", "POST", "/v1/admin/tenants/management/status", admin, {"status": "suspended"})
        call("delete_reserved", "DELETE", "/v1/admin/tenants/management", admin)
        results["reserved_active_after"] = control_plane.STORE.get_tenant("management").active
        call("suspend_other", "POST", "/v1/admin/tenants/managementx/status", admin, {"status": "suspended"})
        call("delete_other", "DELETE", "/v1/admin/tenants/managementx", admin)
        call("restore_other", "POST", "/v1/admin/tenants/managementx/status", admin, {"status": "active"})

        secure = control_plane.CONSOLE_COOKIES_SECURE
        cookie = session.cookie_name(session.COOKIE, secure=secure)
        for name, tenant in (("session_reserved", "management"), ("session_other", "managementx")):
            value, _, _ = session.issue(
                control_plane.SESSION_SECRET, kind="tenant", tenant_id=tenant,
                subject="person", email="p@example.invalid",
            )
            call(name, "GET", "/v1/whoami", headers={"Cookie": f"{cookie}={value}"})
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
        for name in list(environment):
            if name.startswith("SANDBOX_CONTROL_PLANE_OIDC_"):
                environment.pop(name)
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


class ReservedManagementTenantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def response(self, name: str) -> dict:
        return self.results[name]

    def test_the_constant_is_the_name_the_management_plane_files_under(self) -> None:
        self.assertEqual(MANAGEMENT_TENANT, "management")

    def test_an_administrator_cannot_register_the_reserved_name(self) -> None:
        refused = self.response("create_reserved")
        self.assertEqual(refused["status"], 409, refused)
        self.assertIn("reserved", refused["body"]["error"])
        self.assertFalse(self.results["row_after_create_attempt"])
        # A control: the refusal is about the name, not about tenant creation.
        self.assertEqual(self.response("create_other")["status"], 201)
        self.assertIn("reserved", self.results["store_create"])

    def test_an_admin_key_cannot_act_as_the_reserved_tenant(self) -> None:
        self.assertEqual(self.response("admin_as_reserved")["status"], 403)
        self.assertIn("reserved", self.response("admin_as_reserved")["body"]["error"])
        self.assertEqual(self.response("admin_as_other")["status"], 200)
        self.assertEqual(self.response("admin_as_other")["body"]["tenant_id"], "managementx")

    def test_the_management_plane_still_gets_its_row_on_demand(self) -> None:
        self.assertEqual(self.response("admin_workspace")["status"], 201, self.response("admin_workspace"))
        self.assertTrue(self.results["row_after_admin_workspace"])

    def test_no_tenant_key_can_be_issued_for_the_reserved_row(self) -> None:
        # Runs after the row exists, so the refusal is not a 404 for a missing tenant.
        refused = self.response("key_for_reserved")
        self.assertEqual(refused["status"], 400, refused)
        self.assertIn("reserved", refused["body"]["error"])
        self.assertIn("reserved", self.results["store_issue"])
        self.assertEqual(self.response("key_for_other")["status"], 201)

    def test_the_reserved_row_cannot_be_suspended_or_deleted(self) -> None:
        for name in ("suspend_reserved", "delete_reserved"):
            with self.subTest(route=name):
                self.assertEqual(self.response(name)["status"], 403, self.response(name))
                self.assertIn("reserved", self.response(name)["body"]["error"])
        self.assertTrue(self.results["reserved_active_after"])
        # Controls: the same routes still work for an ordinary tenant.
        for name in ("suspend_other", "delete_other", "restore_other"):
            self.assertEqual(self.response(name)["status"], 200, self.response(name))

    def test_a_console_session_for_the_reserved_tenant_is_refused(self) -> None:
        self.assertEqual(self.response("session_reserved")["status"], 403)
        self.assertIn("reserved", self.response("session_reserved")["body"]["error"])
        self.assertEqual(self.response("session_other")["status"], 200)
        self.assertEqual(self.response("session_other")["body"]["tenant_id"], "managementx")


class OidcRoleMappingTests(unittest.TestCase):
    CONFIG = oidc.Config(
        issuer="https://issuer.invalid/realms/sandbox",
        client_id="sandbox-console",
        client_secret="",
        audience="sandbox-control-plane",
        redirect_url="https://sandbox.invalid/v1/auth/oidc/callback",
        scopes=("openid",),
        groups_claim="groups",
        admin_groups=frozenset({"platform-operators"}),
        tenant_claim="sandbox_tenant",
        allow_insecure_http=False,
    )

    def test_a_tenant_claim_naming_the_reserved_tenant_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            oidc.role_of(self.CONFIG, {"groups": [], "sandbox_tenant": "management"})
        with self.assertRaises(oidc.OidcError):
            oidc.role_of(self.CONFIG, {"groups": [], "sandbox_tenant": " management "})
        self.assertEqual(
            oidc.role_of(self.CONFIG, {"groups": [], "sandbox_tenant": "managementx"}),
            ("tenant", "managementx"),
        )


if __name__ == "__main__":
    unittest.main()
