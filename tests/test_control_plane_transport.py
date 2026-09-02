from __future__ import annotations

import unittest
from unittest import mock

from sandbox_platform import control_plane_transport as transport


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
                transport.urllib.request, "urlopen", return_value=_Response()
            ) as opened,
        ):
            client.request("GET", "/v1/workspaces")
        request = opened.call_args.args[0]
        self.assertEqual(
            request.headers["Traceparent"],
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )
