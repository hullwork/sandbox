"""Who owns an object, and who gets to say so.

Every object key is ``users/<tenant>/<subject>/...``.  That partition is not a
request header that lasts for one call - it is the prefix the bytes live under
for as long as they exist, so a caller that can choose it can choose where its
neighbours' data goes.

Two things are pinned here, and they are one change rather than two:

* a tenant credential can reach the object routes at all.  It could not before,
  and the reason it could not is the second point;
* the ``<tenant>`` segment is derived from the credential and the ``<subject>``
  segment from ``X-Acting-Subject``.  Neither is read from the request, and a
  tenant credential that names an owner anyway is refused rather than corrected.

Opening the routes without deriving the owner would hand every tenant a
cross-tenant write; deriving the owner without opening the routes would leave
the derivation unreachable.  The negative cases below therefore assert on the
object-store command line as well as on the status code: a 403 alone cannot
tell "refused" apart from "carried out under a different owner than the caller
believes", and that second outcome is what this whole mechanism exists to
prevent.
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


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUBJECT_A = "a" * 32
SUBJECT_B = "b" * 32

#: Every route where an owner partition is decided, with a request that reaches
#: the object layer. Checked against ``control_plane.ROUTE_AUTH`` below: a new object
#: route that is not exercised here fails that test rather than shipping
#: unexamined.
LOCATOR = {"scope": "agent", "agent_id": "agent-1", "run_id": "run-1"}
OBJECT_ROUTES: tuple[tuple[str, str, str, dict], ...] = (
    ("ticket", "POST", "/v1/storage/tickets",
     {**LOCATOR, "path": "outputs/report.txt", "operation": "upload",
      "max_bytes": 16}),
    ("put", "POST", "/v1/storage/objects",
     {**LOCATOR, "path": "outputs/report.txt", "content_base64": "aGk="}),
    ("get", "GET", "/v1/storage/objects",
     {**LOCATOR, "path": "outputs/report.txt"}),
    ("list", "GET", "/v1/storage/objects/list", dict(LOCATOR)),
    ("stat", "GET", "/v1/storage/objects/stat",
     {**LOCATOR, "path": "outputs/report.txt"}),
    ("versions", "GET", "/v1/storage/objects/versions",
     {**LOCATOR, "path": "outputs/report.txt"}),
    ("delete", "DELETE", "/v1/storage/objects",
     {**LOCATOR, "path": "outputs/report.txt"}),
)

PROBE = textwrap.dedent(
    """
    import json
    import threading
    import urllib.error
    import urllib.parse
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

    # The single choke point every object operation goes through. Recording it
    # is what makes "nothing was written" an assertion rather than an
    # inference from a status code.
    MC_CALLS = []

    def fake_run_mc(*args, input_bytes=None, max_output_bytes=0):
        MC_CALLS.append(list(args))
        if args[0] == "ls":
            return b""
        if args[0] == "stat":
            return json.dumps({"size": 2, "etag": "e", "metadata": {}}).encode()
        return b"hi"

    control_plane.run_mc = fake_run_mc

    def fake_volume(method, path, payload=None, query=None, timeout=40):
        if method == "GET" and path == "/v1/workspaces":
            return 200, b'{"workspaces": []}', "application/json"
        return 200, b'{"created": true}', "application/json"

    control_plane.volume_agent_request = fake_volume
    control_plane.STORE.ensure_schema()

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = {}

    def call(name, method, path, token, body=None, headers=None, query=None):
        before = len(MC_CALLS)
        if query:
            path = f"{path}?{urllib.parse.urlencode(query)}"
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
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        results[name] = {
            "status": status,
            "body": parsed,
            "mc": MC_CALLS[before:],
        }
        return results[name]

    def object_call(name, method, path, token, payload, headers=None):
        if method == "POST":
            return call(name, method, path, token, payload, headers)
        return call(name, method, path, token, None, headers, query=payload)

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
        call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
        call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
        key_a = call(
            "key_a", "POST", "/v1/admin/tenants/tenant-a/keys", admin,
            {"label": "a", "permissions": ["act_as_subjects"]},
        )["body"]["api_key"]
        key_b = call(
            "key_b", "POST", "/v1/admin/tenants/tenant-b/keys", admin,
            {"label": "b", "permissions": ["act_as_subjects"]},
        )["body"]["api_key"]
        admin_key = call(
            "admin_key", "POST", "/v1/admin/keys", admin, {"label": "management"}
        )["body"]["api_key"]

        acting_a = {"X-Acting-Subject": SUBJECT_A}
        for name, method, path, payload in OBJECT_ROUTES:
            # Allowed: a tenant credential naming a subject and no owner.
            object_call(f"derived_{name}", method, path, key_a, payload, acting_a)
            # Refused: the same credential reaching into another tenant.
            object_call(
                f"forged_{name}", method, path, key_a,
                {**payload, "owner": f"tenant-b/{SUBJECT_B}"}, acting_a,
            )
            # Refused as well: naming the partition it would have been given
            # anyway. Accepting the matching case teaches the caller that the
            # field decides, and it finds out otherwise on the request where
            # the two differ.
            object_call(
                f"self_named_{name}", method, path, key_a,
                {**payload, "owner": f"tenant-a/{SUBJECT_A}"}, acting_a,
            )
            # No subject to build a partition from.
            object_call(f"no_subject_{name}", method, path, key_a, payload)
            # The management plane has no tenant of its own, so naming an owner
            # is the only way it can act for one. Unchanged by this.
            object_call(
                f"management_{name}", method, path, admin_key,
                {**payload, "owner": "legacy-host/alice"},
            )
            # A second tenant, same subject: the prefixes must not meet.
            object_call(
                f"other_tenant_{name}", method, path, key_b, payload,
                {"X-Acting-Subject": SUBJECT_A},
            )

        # An unregistered owner tenant segment is still a caller-supplied
        # string; a tenant credential may not smuggle one in either.
        object_call(
            "forged_unregistered", "POST", "/v1/storage/objects", key_a,
            {**LOCATOR, "path": "outputs/report.txt", "content_base64": "aGk=",
             "owner": "legacy-host/alice"},
            acting_a,
        )

        # The workspace token carries the same partition, because it is what
        # object import/export runs under.
        call("workspace_derived", "POST", "/v1/workspaces", key_a,
             {"session_id": "s-1"}, acting_a)
        call("workspace_forged", "POST", "/v1/workspaces", key_a,
             {"session_id": "s-2", "owner": f"tenant-b/{SUBJECT_B}"}, acting_a)
        call("workspace_no_subject", "POST", "/v1/workspaces", key_a,
             {"session_id": "s-3"})
        call("workspace_management", "POST", "/v1/workspaces", admin_key,
             {"session_id": "s-4", "owner": "legacy-host/alice"})

        results["token_owners"] = {
            name: control_plane.scoped_object_owner(
                control_plane.verify_access_token(
                    (results[name]["body"] or {}).get("access_token") or "",
                    "workspace",
                    (results[name]["body"] or {}).get("workspace_id") or "",
                )
            )
            for name in ("workspace_derived", "workspace_no_subject",
                         "workspace_management")
        }

        # A suspended tenant's outstanding tickets stop working. The owner
        # segment of a derived owner carries no registration row, so this only
        # holds because the suspension lookup resolves it as a tenant id too.
        download = object_call(
            "download_ticket", "POST", "/v1/storage/tickets", key_a,
            {**LOCATOR, "path": "outputs/report.txt", "operation": "download"},
            acting_a,
        )
        ticket = download["body"]["access_token"]
        call("suspend", "POST", "/v1/admin/tenants/tenant-a/status", admin,
             {"status": "suspended"})
        call("suspended_ticket", "GET", "/v1/storage/content", ticket)
        results["mc_alias"] = control_plane.MC_ALIAS
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps(results))
    """
)


def probe_source() -> str:
    return (
        f"SUBJECT_A = {SUBJECT_A!r}\n"
        f"SUBJECT_B = {SUBJECT_B!r}\n"
        f"LOCATOR = {LOCATOR!r}\n"
        f"OBJECT_ROUTES = {OBJECT_ROUTES!r}\n"
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
            timeout=300,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ObjectOwnerDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def response(self, name: str) -> dict:
        return self.results[name]

    @property
    def mc_alias(self) -> str:
        return self.results["mc_alias"]

    def keys_touched(self, response: dict) -> list[str]:
        """Object keys the store client was asked to operate on."""
        return [
            argument.split("/", 2)[2]
            for command in response["mc"]
            for argument in command
            if argument.startswith(self.mc_alias + "/")
        ]

    def test_the_probe_exercised_every_object_route(self) -> None:
        # Without this an emptied route table makes every loop below run zero
        # times and report success.
        self.assertEqual(len(OBJECT_ROUTES), 7)
        for name, _method, _path, _payload in OBJECT_ROUTES:
            self.assertIn(f"derived_{name}", self.results)

    def test_a_tenant_credential_can_reach_the_object_routes(self) -> None:
        """The half that used to be a flat 403 on every one of these."""
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"derived_{name}")
                self.assertIn(response["status"], (200, 201), response)

    def test_the_owner_is_built_from_the_credential_and_the_header(self) -> None:
        prefix = f"users/tenant-a/{SUBJECT_A}/"
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"derived_{name}")
                if name == "ticket":
                    # A ticket touches no object yet; its key is in the claims.
                    self.assertTrue(
                        response["body"]["object"]["key"].startswith(prefix),
                        response["body"]["object"]["key"],
                    )
                    continue
                touched = self.keys_touched(response)
                self.assertTrue(touched, response)
                for key in touched:
                    self.assertTrue(key.startswith(prefix), key)

    def test_a_tenant_credential_may_not_name_another_tenants_owner(self) -> None:
        """Acceptance criterion 5, on the persisted half of the identity.

        Status **and** command line: a bare status assertion cannot distinguish
        a refusal from the request being carried out under the caller's own
        partition, and the second outcome is a silent disagreement about where
        the bytes went rather than a security hole - which is exactly why it
        would survive review.
        """
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"forged_{name}")
                self.assertEqual(response["status"], 403, response)
                self.assertEqual(response["mc"], [], "the operation ran anyway")
        smuggled = self.response("forged_unregistered")
        self.assertEqual(smuggled["status"], 403, smuggled)
        self.assertEqual(smuggled["mc"], [])

    def test_naming_its_own_owner_is_refused_too(self) -> None:
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"self_named_{name}")
                self.assertEqual(response["status"], 403, response)
                self.assertEqual(response["mc"], [])

    def test_a_tenant_credential_without_a_subject_has_no_partition(self) -> None:
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"no_subject_{name}")
                self.assertEqual(response["status"], 400, response)
                self.assertEqual(response["mc"], [])

    def test_two_tenants_sharing_a_subject_do_not_share_a_prefix(self) -> None:
        """Isolation by construction rather than by an unguessable value."""
        for name, _method, _path, _payload in OBJECT_ROUTES:
            if name == "ticket":
                continue
            with self.subTest(route=name):
                mine = self.keys_touched(self.response(f"derived_{name}"))
                theirs = self.keys_touched(self.response(f"other_tenant_{name}"))
                self.assertTrue(mine and theirs)
                self.assertEqual(set(mine) & set(theirs), set())
                for key in theirs:
                    self.assertTrue(
                        key.startswith(f"users/tenant-b/{SUBJECT_A}/"), key
                    )

    def test_the_management_plane_still_names_the_owner_itself(self) -> None:
        """The identity that has no tenant of its own must keep working.

        It is the credential an operator uses on data that predates any of
        this, and an owner segment belonging to no tenant is the normal case
        for it rather than an edge one.
        """
        for name, _method, _path, _payload in OBJECT_ROUTES:
            with self.subTest(route=name):
                response = self.response(f"management_{name}")
                self.assertIn(response["status"], (200, 201), response)
                if name == "ticket":
                    self.assertTrue(
                        response["body"]["object"]["key"].startswith(
                            "users/legacy-host/alice/"
                        )
                    )
                    continue
                touched = self.keys_touched(response)
                self.assertTrue(touched, response)
                for key in touched:
                    self.assertTrue(key.startswith("users/legacy-host/alice/"), key)


class WorkspaceTokenOwnerTests(unittest.TestCase):
    """The same partition, on the token that object import/export runs under.

    A workspace token carries the owner it may move objects for. Leaving that
    one caller-supplied would have kept the whole derivation optional: name an
    owner here, and every later object operation trusts the claim.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = ObjectOwnerDerivationTests.results

    def test_the_token_is_bound_to_the_derived_partition(self) -> None:
        created = self.results["workspace_derived"]
        self.assertEqual(created["status"], 201, created)
        self.assertEqual(
            self.results["token_owners"]["workspace_derived"],
            f"tenant-a/{SUBJECT_A}",
        )

    def test_a_named_owner_is_refused_here_as_well(self) -> None:
        refused = self.results["workspace_forged"]
        self.assertEqual(refused["status"], 403, refused)

    def test_a_caller_that_names_no_subject_keeps_working_without_one(self) -> None:
        """Deliberately not a refusal, unlike the object routes.

        A token with no owner claim cannot touch object storage at all -
        ``bind_object_owner`` refuses outright - so refusing here would break
        callers that never touch it, to close something already closed.
        """
        created = self.results["workspace_no_subject"]
        self.assertEqual(created["status"], 201, created)
        self.assertIsNone(self.results["token_owners"]["workspace_no_subject"])

    def test_the_management_plane_still_binds_the_owner_it_names(self) -> None:
        created = self.results["workspace_management"]
        self.assertEqual(created["status"], 201, created)
        self.assertEqual(
            self.results["token_owners"]["workspace_management"],
            "legacy-host/alice",
        )


class SuspensionReachesDerivedOwnersTests(unittest.TestCase):
    """Deactivating a tenant must also stop the tickets it already handed out.

    The gate that does this resolves an owner tenant segment through the
    registration table, which a derived owner never has a row in. Without the
    direct tenant lookup alongside it, every object a tenant credential wrote
    would answer "nobody knows who owns this" and be let through.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = ObjectOwnerDerivationTests.results

    def test_an_outstanding_ticket_stops_working_when_the_tenant_is_suspended(
        self,
    ) -> None:
        self.assertEqual(self.results["suspend"]["status"], 200,
                         self.results["suspend"])
        refused = self.results["suspended_ticket"]
        self.assertEqual(refused["status"], 403, refused)
        self.assertEqual(refused["body"], {"error": "tenant is suspended"})
        self.assertEqual(refused["mc"], [])


class RouteCoverageTests(unittest.TestCase):
    """Every object route in the manifest is exercised above.

    The manifest is what a new route has to be added to (test_route_completeness
    enforces that), so tying this table to it is what stops the next object
    route from arriving with its owner unexamined - the way this whole group
    arrived at "the owner is whatever the caller said".
    """

    def test_the_route_table_covers_every_storage_route_in_the_manifest(self) -> None:
        tree = ast.parse((ROOT / "control_plane/core.py").read_text(encoding="utf-8"))
        manifest = None
        for node in tree.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "ROUTE_AUTH"
            ):
                manifest = ast.literal_eval(node.value)
        self.assertIsNotNone(manifest, "control_plane.ROUTE_AUTH not found")
        declared = {
            (method, path)
            for method, path, _ in manifest
            if path.startswith("/v1/storage/")
            # The content route is spent with an object ticket, whose owner was
            # already decided when the ticket was signed.
            and path != "/v1/storage/content"
        }
        covered = {(method, path) for _name, method, path, _ in OBJECT_ROUTES}
        self.assertEqual(declared - covered, set())


if __name__ == "__main__":
    unittest.main()
