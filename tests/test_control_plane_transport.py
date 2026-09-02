from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from sandbox_platform import control_plane_transport as transport
from sandbox_platform import sandbox_client


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return b"{}"


class TracePropagationTests(unittest.TestCase):
    def test_embedding_app_context_is_injected_without_owning_exporter(self) -> None:
        class Propagator:
            @staticmethod
            def inject(*, carrier):
                carrier["traceparent"] = (
                    "00-4bf92f3577b34da6a3ce929d0e0e4736-"
                    "00f067aa0ba902b7-01"
                )

        client = transport.ControlPlaneTransport("http://sandbox", "token")
        with (
            mock.patch.object(transport, "_otel_propagate", Propagator()),
            mock.patch.object(
                transport, "urlopen_without_redirects", return_value=_Response()
            ) as opened,
        ):
            client.request("GET", "/v1/workspaces")
        request = opened.call_args.args[0]
        self.assertEqual(
            request.headers["Traceparent"],
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )


class _RedirectPair:
    """An origin that answers every GET with 302 to a target that records what arrives."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        seen = self.seen

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        target_port = self.target.server_port

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{target_port}/elsewhere"
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        for server in (self.origin, self.target):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @property
    def origin_url(self) -> str:
        return f"http://127.0.0.1:{self.origin.server_port}"

    def close(self) -> None:
        for server in (self.origin, self.target):
            server.shutdown()
            server.server_close()


class RedirectRefusalTests(unittest.TestCase):
    """A 3xx must end the request, not carry the bearer token to a new host.

    urllib's default handler re-sends the original headers to the Location.
    The Control Plane never redirects, so any 3xx is somebody in between, and
    the criterion is what the *target* received: nothing.
    """

    def setUp(self) -> None:
        self.pair = _RedirectPair()
        self.addCleanup(self.pair.close)

    def test_a_redirect_is_an_error_and_the_token_stays_home(self) -> None:
        client = transport.ControlPlaneTransport(self.pair.origin_url, "SECRET-TOKEN")
        with self.assertRaises(transport.ControlPlaneError) as caught:
            client.request("GET", "/v1/whoami")
        self.assertEqual(caught.exception.status, 302)
        self.assertIn("does not follow redirects", str(caught.exception))
        self.assertEqual(self.pair.seen, [])

    def test_the_object_download_ticket_is_not_carried_across_a_redirect(self) -> None:
        manager = sandbox_client.SandboxManager()
        with (
            mock.patch.dict(
                sandbox_client.os.environ,
                {
                    "SANDBOX_CONTROL_PLANE_URL": self.pair.origin_url,
                    "SANDBOX_TOKEN": "admin-token",
                },
            ),
            mock.patch.object(
                manager,
                "issue_object_ticket",
                return_value={"access_token": "TICKET-TOKEN"},
            ),
        ):
            with self.assertRaises(transport.ControlPlaneError) as caught:
                manager.open_object(
                    {"object_id": "o1"}, max_bytes=10, content_type="text/plain"
                )
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(self.pair.seen, [])
