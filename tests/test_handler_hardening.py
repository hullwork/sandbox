"""Robustness of ``ApiHandler`` at the request boundary, over real HTTP.

Each class boots the SQLite-backed Control Plane of
``tests/control_plane_probe.py`` and speaks to it the way a misbehaving or
merely unusual client would:

* ``POST /v1/admin/tenants`` with a quota that is ``null``, a list, a string
  or a boolean is a 400, not a dropped connection (``int(None)`` raised a
  TypeError the dispatcher did not translate, and the thread died);
* the Grafana panel proxy answers 400 to a ``Content-Length`` it cannot
  parse or that is negative, instead of dying or waiting for EOF;
* the access log records the request path and never the query string, so
  the OIDC callback's ``code`` and ``state`` stay out of the process log, and
  no rejection path writes the presented bearer credential either;
* identity is per request, not per connection: under HTTP/1.1 keep-alive one
  handler instance serves several requests, and the second must not inherit
  the first one's tenant (``handle_one_request`` resets every field the
  authentication path writes).
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from control_plane_probe import run_probe  # noqa: E402


class AdminQuotaInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results, _ = run_probe(
            """
            for label, value in (
                ("null", None), ("list", [1]), ("string", "5"), ("bool", True),
                ("object", {"n": 1}), ("float", 1.5),
            ):
                call(f"ws_{label}", "POST", "/v1/admin/tenants", admin, {"id": f"ws-{label}", "max_workspaces": value})
                call(f"rt_{label}", "POST", "/v1/admin/tenants", admin, {"id": f"rt-{label}", "max_runtimes": value})
            call("zero", "POST", "/v1/admin/tenants", admin, {"id": "zero", "max_workspaces": 0})
            call("good", "POST", "/v1/admin/tenants", admin, {"id": "good", "max_workspaces": 3, "max_runtimes": 2})
            call("after", "GET", "/v1/whoami", admin)
            results["tenants"] = sorted(t.id for t in control_plane.STORE.list_tenants())
            """
        )

    def test_a_quota_of_the_wrong_type_is_a_400_with_a_body(self) -> None:
        for label in ("null", "list", "string", "bool", "object", "float"):
            for field in ("ws", "rt"):
                with self.subTest(field=field, value=label):
                    response = self.results[f"{field}_{label}"]
                    self.assertEqual(response["status"], 400, response)
                    self.assertIn("must be an integer", response["body"]["error"])

    def test_the_server_keeps_answering_afterwards(self) -> None:
        self.assertEqual(self.results["after"]["status"], 200)
        self.assertEqual(self.results["zero"]["status"], 409, self.results["zero"])
        good = self.results["good"]
        self.assertEqual(good["status"], 201, good)
        self.assertEqual((good["body"]["max_workspaces"], good["body"]["max_runtimes"]), (3, 2))
        self.assertEqual(self.results["tenants"], ["good"])


class GrafanaContentLengthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results, _ = run_probe(
            """
            def raw(name, content_length):
                sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=10)
                try:
                    sock.sendall(
                        "POST /grafana/api/ds/query HTTP/1.0\\r\\n"
                        f"Host: 127.0.0.1:{server.server_port}\\r\\n"
                        f"Authorization: Bearer {admin}\\r\\n"
                        "Sec-Fetch-Site: same-origin\\r\\n"
                        "Content-Type: application/json\\r\\n"
                        f"Content-Length: {content_length}\\r\\n\\r\\n".encode()
                    )
                    chunks = []
                    while True:
                        data = sock.recv(65536)
                        if not data:
                            break
                        chunks.append(data)
                except OSError as exc:
                    results[name] = {"status": None, "error": str(exc)}
                    return
                finally:
                    sock.close()
                head, _, body = b"".join(chunks).partition(b"\\r\\n\\r\\n")
                status_line = head.split(b"\\r\\n", 1)[0].decode("latin-1")
                results[name] = {
                    "status": int(status_line.split()[1]) if status_line else None,
                    "body": json.loads(body) if body.startswith(b"{") else body.decode("latin-1"),
                }

            raw("unparseable", "abc")
            raw("negative", "-1")
            raw("huge", str(10 ** 9))
            call("after", "GET", "/v1/whoami", admin)
            """,
            SANDBOX_GRAFANA_URL="http://127.0.0.1:1",
            SANDBOX_GRAFANA_TOKEN="grafana-sa-token",
            SANDBOX_GRAFANA_DATASOURCE_UID="ds-test",
        )

    def test_an_unparseable_or_negative_content_length_is_a_400(self) -> None:
        for name in ("unparseable", "negative"):
            with self.subTest(header=name):
                response = self.results[name]
                self.assertEqual(response["status"], 400, response)
                self.assertIn("Content-Length", response["body"]["error"])

    def test_the_size_bound_and_the_server_are_intact(self) -> None:
        self.assertEqual(self.results["huge"]["status"], 413, self.results["huge"])
        self.assertEqual(self.results["after"]["status"], 200)


class AccessLogQueryTests(unittest.TestCase):
    SECRET = "SUPERSECRETCODE-4f1e"
    BEARER = "sk_BEARERSECRET7c2d9e1f0a3b4c5d6e7f8091a2b3c4d5e6f7"

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.stdout = run_probe(
            f"""
            call("whoami", "GET", "/v1/whoami?code={cls.SECRET}&state=abc", admin)
            call("callback", "GET", "/v1/auth/oidc/callback?code={cls.SECRET}&state=abc")
            call("files", "GET", "/v1/workspaces?filter={cls.SECRET}", admin)
            # A credential that does not exist, on a protected route, on an
            # admin route and on a scoped route: every rejection path logs
            # something, and none of it may be the credential.
            call("bearer_whoami", "GET", "/v1/whoami", "{cls.BEARER}")
            call("bearer_admin", "GET", "/v1/admin/tenants", "{cls.BEARER}")
            call("bearer_files", "GET", "/v1/workspaces/ws-0123456789ab/files/list?path=.", "{cls.BEARER}")
            call("bearer_tenant", "GET", "/v1/whoami", "{cls.BEARER}", headers={{"X-Sandbox-Tenant": "acme"}})
            call("bearer_subject", "GET", "/v1/whoami", "{cls.BEARER}", headers={{"X-Acting-Subject": "a" * 32}})
            call("bearer_scoped", "POST", "/v1/sandboxes/sb-0123456789ab/mcp", "{cls.BEARER}", {{}})
            call("bearer_ticket", "GET", "/v1/storage/content", "{cls.BEARER}")
            """
        )

    def test_the_requests_were_served_and_logged(self) -> None:
        self.assertEqual(self.results["whoami"]["status"], 200)
        self.assertIsNotNone(self.results["callback"]["status"])
        lines = [line for line in self.stdout.splitlines() if "/v1/whoami" in line]
        self.assertTrue(lines, self.stdout)
        self.assertTrue(
            any('"GET /v1/whoami HTTP/1.1" 200' in line for line in lines), lines
        )
        self.assertTrue(
            any('/v1/auth/oidc/callback ' in line for line in self.stdout.splitlines()), self.stdout
        )

    def test_the_query_string_never_reaches_the_log(self) -> None:
        log_lines = [line for line in self.stdout.splitlines() if not line.startswith("RESULTS ")]
        self.assertEqual([line for line in log_lines if self.SECRET in line], [])
        self.assertEqual([line for line in log_lines if "?" in line], [])

    def test_a_bearer_credential_never_reaches_the_log(self) -> None:
        for name in ("bearer_whoami", "bearer_admin", "bearer_files", "bearer_tenant",
                     "bearer_subject", "bearer_scoped", "bearer_ticket"):
            with self.subTest(route=name):
                self.assertEqual(self.results[name]["status"], 401, self.results[name])
        log_lines = [line for line in self.stdout.splitlines() if not line.startswith("RESULTS ")]
        self.assertTrue(log_lines)
        for fragment in (self.BEARER, self.BEARER[3:20], "BEARERSECRET"):
            self.assertEqual([line for line in log_lines if fragment in line], [], fragment)


class KeepAliveIdentityTests(unittest.TestCase):
    """Several requests on one TCP connection each carry their own identity.

    ``ApiHandler.protocol_version`` is HTTP/1.0 today, which closes the
    connection after every response, so the property cannot be observed on
    the handler as shipped. The probe serves a subclass with HTTP/1.1 - the
    one-line change a future performance fix would make - and sends five
    requests down one ``http.client`` connection, checking after each that
    the socket object is still the same one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, _ = run_probe(
            """
            import http.client

            class KeepAliveHandler(api.ApiHandler):
                protocol_version = "HTTP/1.1"

            keepalive = ThreadingHTTPServer(("127.0.0.1", 0), KeepAliveHandler)
            threading.Thread(target=keepalive.serve_forever, daemon=True).start()

            call("tenant_a", "POST", "/v1/admin/tenants", admin, {"id": "tenant-a"})
            call("tenant_b", "POST", "/v1/admin/tenants", admin, {"id": "tenant-b"})
            key_a = call("key_a", "POST", "/v1/admin/tenants/tenant-a/keys", admin, {"label": "a"})["body"]["api_key"]
            key_b = call("key_b", "POST", "/v1/admin/tenants/tenant-b/keys", admin, {"label": "b"})["body"]["api_key"]

            connection = http.client.HTTPConnection("127.0.0.1", keepalive.server_port, timeout=10)
            sequence = []
            sockets = []
            try:
                for label, headers in (
                    ("admin", {"Authorization": f"Bearer {admin}"}),
                    ("anonymous", {}),
                    ("tenant_a", {"Authorization": f"Bearer {key_a}"}),
                    ("tenant_b", {"Authorization": f"Bearer {key_b}"}),
                    ("garbage", {"Authorization": "Bearer sk_not_a_key"}),
                    ("admin_as_b", {"Authorization": f"Bearer {admin}", "X-Sandbox-Tenant": "tenant-b"}),
                    ("admin_again", {"Authorization": f"Bearer {admin}"}),
                ):
                    connection.request("GET", "/v1/whoami", headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    sockets.append(id(connection.sock))
                    sequence.append({
                        "label": label,
                        "status": response.status,
                        "version": response.version,
                        "tenant_id": json.loads(body).get("tenant_id") if response.status == 200 else None,
                        "kind": json.loads(body).get("kind") if response.status == 200 else None,
                    })
            finally:
                connection.close()
                keepalive.shutdown()
                keepalive.server_close()
            results["sequence"] = sequence
            results["one_socket"] = len(set(sockets)) == 1 and sockets[0] != id(None)
            """
        )

    def test_the_requests_really_shared_one_connection(self) -> None:
        self.assertTrue(self.results["one_socket"], self.results)
        self.assertTrue(all(item["version"] == 11 for item in self.results["sequence"]))

    def test_each_request_is_authenticated_on_its_own(self) -> None:
        expected = {
            "admin": (200, None, "break-glass"),
            "anonymous": (401, None, None),
            "tenant_a": (200, "tenant-a", "tenant"),
            "tenant_b": (200, "tenant-b", "tenant"),
            "garbage": (401, None, None),
            "admin_as_b": (200, "tenant-b", "break-glass"),
            "admin_again": (200, None, "break-glass"),
        }
        for item in self.results["sequence"]:
            with self.subTest(request=item["label"]):
                self.assertEqual(
                    (item["status"], item["tenant_id"], item["kind"]),
                    expected[item["label"]],
                    item,
                )


if __name__ == "__main__":
    unittest.main()
