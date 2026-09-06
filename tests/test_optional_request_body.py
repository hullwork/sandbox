"""The one route whose request body the contract marks optional must accept none.

``POST /v1/workspaces/{id}/checkpoints/{id}/restore`` takes a body carrying a
single optional ``sha256``. The OpenAPI says ``requestBody: required: false``;
the handler called ``read_json`` anyway, so a client that sent no body was told
``Content-Length is required`` and one that sent ``Content-Length: 0`` was told
``request body must be valid JSON``. Both were observed against a live
deployment; ``-d '{}'`` was the undocumented way through.

The probe runs in a subprocess: importing ``control_plane.api`` needs a
configured environment, and setting one in this process would follow every
module imported after it.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    """
    import io
    import json

    from control_plane import api

    class Headers:
        def __init__(self, values):
            self._values = values

        def get(self, name, default=None):
            return self._values.get(name, default)

    class Probe:
        # The real methods, on a request that owns nothing but its headers and
        # its body. read_optional_json delegates to read_json, so both come
        # from ApiHandler rather than being restated here.
        read_json = api.ApiHandler.read_json
        read_optional_json = api.ApiHandler.read_optional_json

        def __init__(self, headers, body=b""):
            self.headers = Headers(headers)
            self.rfile = io.BytesIO(body)

    def attempt(headers, body=b""):
        try:
            return {"value": Probe(headers, body).read_optional_json()}
        except ValueError as exc:
            return {"error": str(exc)}

    body = b'{"sha256": "ab"}'
    print(json.dumps({
        "absent": attempt({}),
        "zero_length": attempt({"Content-Length": "0"}),
        "present": attempt({"Content-Length": str(len(body))}, body),
        "malformed": attempt({"Content-Length": "8"}, b"not json"),
        "chunked": attempt({"Transfer-Encoding": "chunked"}),
    }))
    """
)


def run_probe() -> dict:
    # The volume role is the one role core.py does not build a KubeClient for,
    # and it relaxes the configuration gate to a single variable. Nothing here
    # touches Kubernetes.
    environment = {
        **os.environ,
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "PYTHONPATH": str(ROOT),
    }
    environment.pop("KUBERNETES_SERVICE_HOST", None)
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


class OptionalRequestBodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def test_no_body_reads_as_empty(self) -> None:
        self.assertEqual(self.results["absent"], {"value": {}})

    def test_zero_length_body_reads_as_empty(self) -> None:
        self.assertEqual(self.results["zero_length"], {"value": {}})

    def test_a_present_body_is_still_parsed(self) -> None:
        self.assertEqual(self.results["present"], {"value": {"sha256": "ab"}})

    def test_a_present_but_malformed_body_still_fails(self) -> None:
        self.assertIn("error", self.results["malformed"], self.results["malformed"])

    def test_a_chunked_body_is_refused_rather_than_read_as_absent(self) -> None:
        # This server never decodes chunked bodies. Silently calling one "no
        # body" would drop the sha256 the caller sent and restore the archive
        # with no integrity check, and the request would look like it worked.
        self.assertIn("error", self.results["chunked"], self.results["chunked"])


class ContractAndHandlerAgreeTests(unittest.TestCase):
    def test_every_optional_body_route_uses_the_optional_reader(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "contracts/control-plane-openapi.yaml").read_text(encoding="utf-8")
        )
        optional = [
            (method, path)
            for path, item in spec["paths"].items()
            for method, operation in item.items()
            if isinstance(operation, dict)
            and isinstance(operation.get("requestBody"), dict)
            and operation["requestBody"].get("required") is False
        ]
        self.assertEqual(
            optional,
            [("post", "/v1/workspaces/{workspace_id}/checkpoints/{checkpoint_id}/restore")],
            "a new optional-body route needs read_optional_json and a case here",
        )
        source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        start = source.index('r"([a-z0-9][a-z0-9-]{0,62})/restore"')
        # Stop at the next dispatch branch: the block after this one is
        # objects/import, which requires its body and rightly calls read_json.
        block = source[start:source.index("match = self.match_path(", start)]
        self.assertIn("self.read_optional_json()", block)
        self.assertNotIn("self.read_json()", block)


if __name__ == "__main__":
    unittest.main()
