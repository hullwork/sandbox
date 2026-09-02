"""Credential boundaries that a mutation experiment found unguarded.

Three invariants, each exercised over HTTP against the real handler with a
SQLite store (``tests/control_plane_probe.py``), so that removing the check
in the implementation turns a test red rather than passing silently:

* a workspace scoped token is only as good as its signature: a token whose
  signature segment or payload segment was altered, one signed with another
  key, or one minted for a different workspace, is a 401 on the file routes
  before any downstream call (``verify_access_token``); and a validly signed
  token is still refused when its subject names another workspace or
  sandbox, its kind does not fit the route, or its ``exp`` has passed;
* administration routes refuse a tenant key, and refuse an admin key that
  is acting as a tenant through ``X-Sandbox-Tenant`` (``require_admin``);
* a tenant key that sends ``X-Sandbox-Tenant`` is refused, including when the
  header names its own tenant (``reject_tenant_selection``).

The same run validates an administrator's ``/v1/whoami`` (which carries the
``grafana`` block) and ``/healthz`` against the OpenAPI schemas, the two
responses whose contract had drifted from the implementation unnoticed.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from control_plane_probe import run_probe  # noqa: E402
from test_response_schemas import load_spec, validate  # noqa: E402

PROBE_BODY = """
    import hashlib
    import hmac
    import json as _json

    call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
    call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
    key_a = call("key_a", "POST", "/v1/admin/tenants/tenant-a/keys", admin, {"label": "a"})["body"]["api_key"]
    admin_key = call("admin_key", "POST", "/v1/admin/keys", admin, {"label": "ops"})["body"]["api_key"]

    lease = call("lease", "POST", "/v1/workspaces", key_a, {"session_id": "s"})["body"]
    ws = lease["workspace_id"]
    token = lease["access_token"]
    other = call("lease_other", "POST", "/v1/workspaces", key_a, {"session_id": "other"})["body"]
    results["ws"] = ws

    payload, signature = token.split(".", 1)
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    claims = _json.loads(control_plane.b64url_decode(payload))
    forged_claims = dict(claims, sub=ws, exp=claims["exp"] + 1)
    forged_payload = control_plane.b64url_encode(
        _json.dumps(forged_claims, separators=(",", ":"), sort_keys=True).encode()
    )
    other_key_signature = control_plane.b64url_encode(
        hmac.new(b"9" * 32, payload.encode("ascii"), hashlib.sha256).digest()
    )
    bad_tokens = {
        "flipped_signature": f"{payload}.{flipped}",
        "altered_payload": f"{forged_payload}.{signature}",
        "other_key": f"{payload}.{other_key_signature}",
        "other_workspace": other["access_token"],
        "no_signature": payload,
    }
    # Tokens with a valid signature whose claims must still be refused: the
    # subject names another workspace / sandbox, the kind is wrong for the
    # route, or exp is in the past. Minted here the way issue_access_token
    # does, so only the claim under test differs.
    def mint(claims):
        encoded = control_plane.b64url_encode(
            _json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return encoded + "." + control_plane.b64url_encode(
            hmac.new(control_plane.SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
        )

    sb = "sb-0123456789ab"
    results["preseed_runtime"] = control_plane.STORE.admit_runtime("tenant-a", sb, ws, "default", 5)
    now = int(__import__("time").time())
    scoped = {"aud": "sandbox-control-plane", "exp": now + 600}
    claim_tokens = {
        "ws_expired": mint({**scoped, "kind": "workspace", "sub": ws, "exp": now - 10}),
        "ws_other_subject": mint({**scoped, "kind": "workspace", "sub": "ws-ffffffffffff"}),
        "ws_sandbox_kind": mint({**scoped, "kind": "sandbox", "sub": sb}),
    }
    for label, bad in claim_tokens.items():
        call(f"files_{label}", "GET", f"/v1/workspaces/{ws}/files/list?path=.", bad)
        call(f"write_{label}", "POST", f"/v1/workspaces/{ws}/files/write", bad, {"path": "a.txt", "content": "x"})
    mcp_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    call("mcp_valid", "POST", f"/v1/sandboxes/{sb}/mcp", mint({**scoped, "kind": "sandbox", "sub": sb}), mcp_body)
    call("mcp_expired", "POST", f"/v1/sandboxes/{sb}/mcp", mint({**scoped, "kind": "sandbox", "sub": sb, "exp": now - 10}), mcp_body)
    call("mcp_other_subject", "POST", f"/v1/sandboxes/{sb}/mcp", mint({**scoped, "kind": "sandbox", "sub": "sb-ffffffffffff"}), mcp_body)
    call("mcp_workspace_kind", "POST", f"/v1/sandboxes/{sb}/mcp", mint({**scoped, "kind": "workspace", "sub": ws}), mcp_body)
    call("mcp_no_exp", "POST", f"/v1/sandboxes/{sb}/mcp", mint({"aud": "sandbox-control-plane", "kind": "sandbox", "sub": sb}), mcp_body)

    call("files_valid", "GET", f"/v1/workspaces/{ws}/files/list?path=.", token)
    call("write_valid", "POST", f"/v1/workspaces/{ws}/files/write", token, {"path": "a.txt", "content": "x"})
    for label, bad in bad_tokens.items():
        call(f"files_{label}", "GET", f"/v1/workspaces/{ws}/files/list?path=.", bad)
        call(f"write_{label}", "POST", f"/v1/workspaces/{ws}/files/write", bad, {"path": "a.txt", "content": "x"})

    for label, path, method, body in (
        ("list_tenants", "/v1/admin/tenants", "GET", None),
        ("create_tenant", "/v1/admin/tenants", "POST", {"id": "tenant-c"}),
        ("issue_key", "/v1/admin/tenants/tenant-a/keys", "POST", {"label": "x"}),
        ("audit", "/v1/admin/audit", "GET", None),
        ("admin_keys", "/v1/admin/keys", "GET", None),
    ):
        call(f"tenant_key_{label}", method, path, key_a, body)
        call(f"acting_admin_{label}", method, path, admin_key, body, headers={"X-Sandbox-Tenant": "tenant-a"})
    call("admin_key_list_tenants", "GET", "/v1/admin/tenants", admin_key)
    results["tenant_c_exists"] = control_plane.STORE.get_tenant("tenant-c") is not None

    call("tenant_key_selects_self", "GET", "/v1/whoami", key_a, headers={"X-Sandbox-Tenant": "tenant-a"})
    call("tenant_key_selects_other", "GET", "/v1/whoami", key_a, headers={"X-Sandbox-Tenant": "tenant-b"})
    call("tenant_key_selects_self_list", "GET", "/v1/workspaces", key_a, headers={"X-Sandbox-Tenant": "tenant-a"})
    call("tenant_key_plain", "GET", "/v1/whoami", key_a)
    call("admin_key_acting", "GET", "/v1/whoami", admin_key, headers={"X-Sandbox-Tenant": "tenant-a"})

    call("admin_whoami", "GET", "/v1/whoami", admin)
    call("admin_key_whoami", "GET", "/v1/whoami", admin_key)
    call("healthz", "GET", "/healthz")
"""


class CredentialBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()
        cls.results, _ = run_probe(PROBE_BODY)

    def response(self, name: str) -> dict:
        return self.results[name]

    def assert_schema(self, schema: str, value: object) -> None:
        errors = validate(self.spec, {"$ref": f"#/components/schemas/{schema}"}, value)
        self.assertEqual(errors, [], f"{schema}: {value!r}")

    def assert_refused_before_downstream(self, name: str, status: int) -> None:
        response = self.response(name)
        self.assertEqual(response["status"], status, (name, response))
        self.assertEqual(
            (response["kube_calls"], response["volume_calls"]), (0, 0),
            f"{name}: downstream dependency was touched before the credential check",
        )
        self.assert_schema("Error", response["body"])

    def test_fixture_setup_succeeded(self) -> None:
        for name in ("tenant_a", "tenant_b", "key_a", "admin_key", "lease", "lease_other"):
            self.assertEqual(self.response(name)["status"], 201, self.response(name))

    def test_a_valid_scoped_token_reaches_the_runtime_lookup(self) -> None:
        # No runtime exists for the workspace, so the request that passed the
        # token gate is answered 409 after one Kubernetes call - the proof that
        # the 401s below come from the token, not from the missing runtime.
        for name in ("files_valid", "write_valid"):
            with self.subTest(route=name):
                response = self.response(name)
                self.assertEqual(response["status"], 409, response)
                self.assertEqual(response["kube_calls"], 1, response)

    def test_a_scoped_token_with_a_bad_signature_is_a_401(self) -> None:
        for label in ("flipped_signature", "altered_payload", "other_key", "other_workspace", "no_signature"):
            for route in ("files", "write"):
                with self.subTest(token=label, route=route):
                    self.assert_refused_before_downstream(f"{route}_{label}", 401)

    def test_a_scoped_token_for_another_subject_or_kind_is_a_401(self) -> None:
        for label in ("ws_other_subject", "ws_sandbox_kind"):
            for route in ("files", "write"):
                with self.subTest(token=label, route=route):
                    self.assert_refused_before_downstream(f"{route}_{label}", 401)
        for name in ("mcp_other_subject", "mcp_workspace_kind"):
            with self.subTest(route=name):
                self.assert_refused_before_downstream(name, 401)
        # Control: a sandbox token with the right subject passes the gate and
        # reaches the runtime lookup (the fake fails it downstream).
        self.assertTrue(self.results["preseed_runtime"])
        valid = self.response("mcp_valid")
        self.assertNotEqual(valid["status"], 401, valid)
        self.assertGreaterEqual(valid["kube_calls"], 1, valid)

    def test_an_expired_scoped_token_is_a_401(self) -> None:
        for name in ("files_ws_expired", "write_ws_expired", "mcp_expired", "mcp_no_exp"):
            with self.subTest(route=name):
                self.assert_refused_before_downstream(name, 401)

    def test_admin_routes_refuse_a_tenant_key(self) -> None:
        for label in ("list_tenants", "create_tenant", "issue_key", "audit", "admin_keys"):
            with self.subTest(route=label):
                self.assert_refused_before_downstream(f"tenant_key_{label}", 403)
        self.assertFalse(self.results["tenant_c_exists"])

    def test_admin_routes_refuse_an_admin_key_acting_as_a_tenant(self) -> None:
        for label in ("list_tenants", "create_tenant", "issue_key", "audit", "admin_keys"):
            with self.subTest(route=label):
                self.assert_refused_before_downstream(f"acting_admin_{label}", 403)
        # Control: the same key without the header is an administrator.
        self.assertEqual(self.response("admin_key_list_tenants")["status"], 200)

    def test_a_tenant_key_may_not_select_a_tenant_even_its_own(self) -> None:
        for name in ("tenant_key_selects_self", "tenant_key_selects_other", "tenant_key_selects_self_list"):
            with self.subTest(route=name):
                self.assert_refused_before_downstream(name, 403)
        plain = self.response("tenant_key_plain")
        self.assertEqual((plain["status"], plain["body"]["tenant_id"]), (200, "tenant-a"))
        acting = self.response("admin_key_acting")
        self.assertEqual((acting["status"], acting["body"]["tenant_id"]), (200, "tenant-a"))

    def test_administrator_identity_and_health_match_the_contract(self) -> None:
        for name in ("admin_whoami", "admin_key_whoami"):
            response = self.response(name)
            self.assertEqual(response["status"], 200, response)
            self.assertIn("grafana", response["body"], response)
            self.assert_schema("Identity", response["body"])
        health = self.response("healthz")
        self.assertEqual(health["status"], 200, health)
        self.assert_schema("Health", health["body"])


if __name__ == "__main__":
    unittest.main()
