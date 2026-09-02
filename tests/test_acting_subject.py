"""Acting for a subject, and where a request's tenant comes from.

Two rules are tested against a running Control Plane with a SQLite control plane:

  * a credential may name the subject it acts for only if it was issued that
    permission, and a credential without it is **refused** rather than quietly
    treated as acting for itself;
  * the tenant of a request comes from the credential and from nowhere else. A
    caller that names a tenant in a header gets its own tenant anyway.

Both are fail-closed properties, and both are invisible when they break: an
ignored impersonation header files the work under the wrong owner and returns
200, and a tenant header that were honoured would return 200 as well.
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


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUBJECT = "a" * 32
#: The pseudonyms a conforming deriver actually produces, from the vendored
#: cross-repository vector. Run through the live HTTP path rather than only
#: against the regex: the character class is one of several things between the
#: header and a stored workspace, and it is the whole path that has to accept
#: them. See tests/test_acting_subject_vectors.py for the rest of that contract.
VECTOR_SUBJECTS = [
    vector["expected"]
    for vector in json.loads(
        (ROOT / "docs/acting-subject-vectors.json").read_text(encoding="utf-8")
    )["vectors"]
]

PROBE = textwrap.dedent(
    """
    import json
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

        def create_or_get(self, namespace, plural, name, manifest):
            raise kube.KubeError(503, "kubernetes unavailable in this test")

        def delete(self, namespace, plural, name):
            raise kube.KubeError(503, "kubernetes unavailable in this test")

        def patch_annotations(self, namespace, plural, name, annotations):
            raise kube.KubeError(503, "kubernetes unavailable in this test")

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane
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
        return 200, b'{"entries": []}', "application/json"

    control_plane.volume_agent_request = fake_volume
    control_plane.STORE.ensure_schema()

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, method, path, token, body=None, headers=None):
        volume_before = len(VOLUME["workspaces"])
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{base}{path}", method=method, data=data,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        results[name] = {
            "status": status,
            "body": json.loads(raw) if raw else None,
            "workspaces_created": len(VOLUME["workspaces"]) - volume_before,
        }
        return results[name]["body"]

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
        call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
        call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
        plain_key = call(
            "plain_key", "POST", "/v1/admin/tenants/tenant-a/keys", admin,
            {"label": "no-impersonation"},
        )["api_key"]
        issued = call(
            "acting_key", "POST", "/v1/admin/tenants/tenant-a/keys", admin,
            {"label": "impersonating", "permissions": ["act_as_subjects"],
             "expires_in_seconds": 3600},
        )
        acting_key = issued["api_key"]
        call(
            "bad_permission", "POST", "/v1/admin/tenants/tenant-a/keys", admin,
            {"label": "typo", "permissions": ["act_as_everyone"]},
        )
        call(
            "bad_lifetime", "POST", "/v1/admin/tenants/tenant-a/keys", admin,
            {"label": "typo", "expires_in_seconds": 0},
        )

        acting = {"X-Acting-Subject": SUBJECT}
        # 🔴 A key without the permission naming a subject: 403, and nothing
        # created. "Ignored" would show up here as a 201 plus a workspace.
        call("unauthorized_acting", "POST", "/v1/workspaces", plain_key,
             {"session_id": "s-1"}, acting)
        call("authorized_acting", "POST", "/v1/workspaces", acting_key,
             {"session_id": "s-1"}, acting)
        call("no_acting", "POST", "/v1/workspaces", acting_key,
             {"session_id": "s-1"})
        call("whoami_acting", "GET", "/v1/whoami", acting_key, None, acting)
        for name, value in (
            ("uppercase", SUBJECT.upper()),
            ("too_short", "a" * 31),
            ("not_hex", "z" * 32),
            ("with_separator", "a" * 16 + ":" + "a" * 15),
        ):
            call(f"malformed_{name}", "POST", "/v1/workspaces", acting_key,
                 {"session_id": "s-2"}, {"X-Acting-Subject": value})
        call("acting_plus_principal", "POST", "/v1/workspaces", acting_key,
             {"session_id": "s-3", "principal": {"kind": "user", "id": "root"}},
             acting)
        call("break_glass_acting", "GET", "/v1/whoami", admin, None, acting)

        # Every pseudonym a conforming deriver emits must be accepted here.
        for index, pseudonym in enumerate(VECTOR_SUBJECTS):
            call(f"vector_{index}", "POST", "/v1/workspaces", acting_key,
                 {"session_id": "vector-session"},
                 {"X-Acting-Subject": pseudonym})

        # The tenant of a request comes from the credential. A tenant-bound
        # credential naming any tenant - its own included - is refused.
        call("forged_tenant", "POST", "/v1/workspaces", plain_key,
             {"session_id": "s-4"}, {"X-Sandbox-Tenant": "tenant-b"})
        call("own_tenant_named", "POST", "/v1/workspaces", plain_key,
             {"session_id": "s-5"}, {"X-Sandbox-Tenant": "tenant-a"})
        call("forged_tenant_read", "GET", "/v1/whoami", plain_key, None,
             {"X-Sandbox-Tenant": "tenant-b"})
        # The management plane is a different identity: naming a tenant is the
        # only way an admin credential can act for one, so it still works.
        # 🔴 Exercised with a real admin **key**, not the break-glass token:
        # that token is recognized several branches earlier, so asserting on it
        # would leave the path production actually uses untested.
        admin_key = call(
            "admin_key", "POST", "/v1/admin/keys", admin, {"label": "management"}
        )["api_key"]
        call("admin_key_acts_for_tenant", "GET", "/v1/whoami", admin_key, None,
             {"X-Sandbox-Tenant": "tenant-a"})
        call("break_glass_acts_for_tenant", "GET", "/v1/whoami", admin, None,
             {"X-Sandbox-Tenant": "tenant-a"})
        results["tenant_a_workspaces"] = [
            row["workspace_id"] for row in control_plane.STORE.list_workspaces("tenant-a")
        ]
        results["tenant_b_workspaces"] = [
            row["workspace_id"] for row in control_plane.STORE.list_workspaces("tenant-b")
        ]

        expired_plaintext, _ = control_plane.STORE.issue_api_key(
            "tenant-a", "already-expired", expires_in_seconds=60,
            now=int(time.time()) - 3600,
        )
        call("expired_key", "GET", "/v1/whoami", expired_plaintext)
        live_plaintext, _ = control_plane.STORE.issue_api_key(
            "tenant-a", "still-valid", expires_in_seconds=3600
        )
        call("live_key", "GET", "/v1/whoami", live_plaintext)
        results["listed_keys"] = [
            {"label": row["label"], "permissions": row["permissions"],
             "expires": row["expires_at"] is not None}
            for row in control_plane.STORE.list_api_keys("tenant-a")
        ]
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)


def probe_source() -> str:
    return (
        f"SUBJECT = {SUBJECT!r}\n"
        f"VECTOR_SUBJECTS = {VECTOR_SUBJECTS!r}\n"
    ) + PROBE


def run_probe() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-admin-token",
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
            [sys.executable, "-c", probe_source()],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ActingSubjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def response(self, name: str) -> dict:
        return self.results[name]

    def test_an_unauthorized_key_naming_a_subject_is_refused(self) -> None:
        """Acceptance criterion 4: 403, and not silently ignored."""
        refused = self.response("unauthorized_acting")
        self.assertEqual(refused["status"], 403, refused)
        self.assertEqual(refused["body"], self.REFUSAL)
        self.assertEqual(
            refused["workspaces_created"], 0,
            "the request was carried out anyway, so the header was ignored",
        )

    def test_an_authorized_key_acts_inside_its_own_tenant(self) -> None:
        allowed = self.response("authorized_acting")
        self.assertEqual(allowed["status"], 201, allowed)
        self.assertEqual(allowed["workspaces_created"], 1)
        # Same session id, same key, different subject: a different workspace.
        # The pseudonym is what separates one end user from another.
        self.assertNotEqual(
            allowed["body"]["workspace_id"],
            self.response("no_acting")["body"]["workspace_id"],
        )
        identity = self.response("whoami_acting")
        self.assertEqual(identity["status"], 200, identity)
        self.assertEqual(identity["body"]["acting_subject"], "a" * 32)
        self.assertEqual(identity["body"]["tenant_id"], "tenant-a")

    def test_a_subject_outside_the_shape_is_refused(self) -> None:
        for name in ("uppercase", "too_short", "not_hex", "with_separator"):
            with self.subTest(subject=name):
                response = self.response(f"malformed_{name}")
                self.assertEqual(response["status"], 400, response)
                self.assertEqual(response["workspaces_created"], 0)

    def test_a_subject_and_a_body_principal_cannot_both_be_given(self) -> None:
        response = self.response("acting_plus_principal")
        self.assertEqual(response["status"], 400, response)
        self.assertEqual(response["workspaces_created"], 0)

    #: The refusal every credential that cannot act for a subject must give.
    #: Published in docs/AUTH.md, so it is asserted verbatim rather than by
    #: status code alone.
    REFUSAL = {"error": "this credential may not act for a subject"}

    def test_the_break_glass_token_cannot_act_for_a_subject(self) -> None:
        response = self.response("break_glass_acting")
        self.assertEqual(response["status"], 403, response)
        # 🔴 The body, not just the status. There are two code paths that emit
        # this refusal - one for API keys, one for everything else - and a
        # status-only assertion cannot tell that one of them drifted. Found by
        # running a mutation that had been skipped as "anchor not unique":
        # changing the message on either path left every test green.
        self.assertEqual(response["body"], self.REFUSAL)


class SharedVectorAcceptanceTests(unittest.TestCase):
    """🔴 The upstream derives correctly and this side refuses it.

    That is the failure this platform can actually have - it never derives a
    pseudonym, only receives one - and it is invisible from the deriving side,
    which sees a well-formed value go out and a rejection come back with no
    indication that the value itself was the problem.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = ActingSubjectTests.results

    def test_the_probe_actually_exercised_the_vector(self) -> None:
        # Without this, an emptied vector file makes the loop below run zero
        # times and report success.
        self.assertGreaterEqual(len(VECTOR_SUBJECTS), 5)

    def test_every_derived_pseudonym_is_accepted_end_to_end(self) -> None:
        for index, pseudonym in enumerate(VECTOR_SUBJECTS):
            with self.subTest(pseudonym=pseudonym):
                created = self.results[f"vector_{index}"]
                self.assertEqual(created["status"], 201, created)
                self.assertEqual(created["workspaces_created"], 1)

    def test_each_pseudonym_gets_its_own_workspace(self) -> None:
        # One session id, five subjects, five workspaces: the pseudonym is what
        # separates one end user from another inside a tenant.
        workspaces = {
            self.results[f"vector_{index}"]["body"]["workspace_id"]
            for index in range(len(VECTOR_SUBJECTS))
        }
        self.assertEqual(len(workspaces), len(VECTOR_SUBJECTS))


class CredentialTenantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = ActingSubjectTests.results

    def test_a_tenant_key_naming_another_tenant_is_refused(self) -> None:
        """Acceptance criterion 5: refused, and nothing written anywhere."""
        refused = self.results["forged_tenant"]
        self.assertEqual(refused["status"], 403, refused)
        self.assertEqual(
            refused["body"],
            {"error": "this credential is bound to a tenant; "
                      "X-Sandbox-Tenant is not accepted"},
        )
        self.assertEqual(
            refused["workspaces_created"], 0,
            "the request was carried out anyway, so the header was ignored",
        )
        self.assertEqual(self.results["tenant_b_workspaces"], [])

    def test_naming_its_own_tenant_is_refused_as_well(self) -> None:
        """🔴 The matching case is the one that teaches the wrong lesson.

        A caller served correctly while the two values agree concludes the
        header is honoured, and discovers otherwise only on the request where
        they differ - by which point it believes it wrote somewhere it did not.
        """
        refused = self.results["own_tenant_named"]
        self.assertEqual(refused["status"], 403, refused)
        self.assertEqual(refused["workspaces_created"], 0)
        self.assertEqual(
            self.results["forged_tenant_read"]["status"], 403,
            "reads must be refused too, not only writes",
        )

    def test_the_management_plane_may_still_act_for_a_tenant(self) -> None:
        # The rule is about credentials that already carry a tenant. An admin
        # credential carries none, so naming one is how it acts for a tenant at
        # all; breaking that would take the management plane with it.
        for name in ("admin_key_acts_for_tenant", "break_glass_acts_for_tenant"):
            with self.subTest(credential=name):
                acting = self.results[name]
                self.assertEqual(acting["status"], 200, acting)
                self.assertEqual(acting["body"]["tenant_id"], "tenant-a")
        # 🔴 `kind` still reads "admin" here: it describes the credential, not
        # the scope this request runs in. What actually narrows is
        # `capabilities`, which is why the contract tells clients to decide from
        # capabilities and never from kind. Asserted so the two cannot drift.
        identity = self.results["admin_key_acts_for_tenant"]["body"]
        self.assertEqual(identity["kind"], "admin")
        self.assertNotIn("tenants:write", identity["capabilities"])
        self.assertNotIn("keys:write", identity["capabilities"])


class ApiKeyLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = ActingSubjectTests.results

    def test_an_expired_key_no_longer_authenticates(self) -> None:
        self.assertEqual(self.results["expired_key"]["status"], 401)
        self.assertEqual(self.results["live_key"]["status"], 200)

    def test_an_unknown_permission_is_rejected_at_issuance(self) -> None:
        # Storing it and never acting on it would read as granted in every
        # listing while doing nothing.
        self.assertEqual(self.results["bad_permission"]["status"], 400)
        self.assertEqual(self.results["bad_lifetime"]["status"], 400)

    def test_issued_keys_report_their_permissions_and_expiry(self) -> None:
        issued = self.results["acting_key"]["body"]
        self.assertEqual(issued["permissions"], ["act_as_subjects"])
        self.assertIsNotNone(issued["expires_at"])
        listed = {row["label"]: row for row in self.results["listed_keys"]}
        self.assertEqual(
            listed["impersonating"]["permissions"], ["act_as_subjects"]
        )
        self.assertEqual(listed["no-impersonation"]["permissions"], [])
        self.assertTrue(listed["still-valid"]["expires"])


if __name__ == "__main__":
    unittest.main()
