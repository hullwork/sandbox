"""A SQLite-backed Control Plane in a subprocess, for behaviour-level tests.

Same construction as ``test_api_authorization``: Kubernetes and the volume
agent are replaced by in-process fakes (every mutating Kubernetes call fails
with 503, so a request that is refused first is proven to be refused before
any downstream call), the real ``api.ApiHandler`` serves on a kernel-assigned
port, and the break-glass token is the administrator.

A test supplies the body of the probe (Python source, run with ``call``,
``results``, ``admin``, ``api``, ``control_plane`` and ``server`` in scope) and
gets back the ``results`` dict plus the subprocess output (stdout followed by
stderr), which carries the handler's access log lines.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]

PRELUDE = textwrap.dedent(
    """
    import json
    import socket
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

    from control_plane import api, session
    from control_plane import core as control_plane
    from control_plane.store import StoreError
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

    def call(name, method, path, token=None, body=None, headers=None, raw_body=None):
        before = (len(FakeKube.calls), len(VOLUME["calls"]))
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode() if body is not None else None
        )
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
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            status, raw = None, json.dumps({"transport_error": str(exc)}).encode()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        results[name] = {
            "status": status,
            "body": parsed,
            "kube_calls": len(FakeKube.calls) - before[0],
            "volume_calls": len(VOLUME["calls"]) - before[1],
        }
        return results[name]

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    try:
    """
)

EPILOGUE = textwrap.dedent(
    """
    finally:
        server.shutdown()
        server.server_close()
    print("RESULTS " + json.dumps(results), flush=True)
    """
)


def environment(directory: str, **overrides: str) -> dict[str, str]:
    env = {
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
        "PYTHONDONTWRITEBYTECODE": "1",
        **overrides,
    }
    env.pop("SANDBOX_CONTROL_PLANE_ROLE", None)
    for name in list(env):
        if name.startswith("SANDBOX_CONTROL_PLANE_OIDC_") and name not in overrides:
            env.pop(name)
    return env


def run_probe(body: str, **overrides: str) -> tuple[dict, str]:
    """Run ``body`` inside the probe; return (results, stdout + stderr)."""
    source = PRELUDE + textwrap.indent(textwrap.dedent(body), "    ") + EPILOGUE
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=environment(directory, **overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULTS ")]
    if len(lines) != 1:
        raise AssertionError("probe printed no RESULTS line:\n" + result.stdout + result.stderr)
    return json.loads(lines[0][len("RESULTS "):]), result.stdout + result.stderr
