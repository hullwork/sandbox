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
  the OIDC callback's ``code`` and ``state`` stay out of the process log.
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

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.stdout = run_probe(
            f"""
            call("whoami", "GET", "/v1/whoami?code={cls.SECRET}&state=abc", admin)
            call("callback", "GET", "/v1/auth/oidc/callback?code={cls.SECRET}&state=abc")
            call("files", "GET", "/v1/workspaces?filter={cls.SECRET}", admin)
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


if __name__ == "__main__":
    unittest.main()
