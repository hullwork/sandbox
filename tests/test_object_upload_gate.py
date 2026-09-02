"""The object-queue gate is taken before a body is spooled, and /tmp is sized to it.

Three paths spool a large body to ``/tmp`` before handing it to the object
store: the ticket upload (``receive_object_content``), the workspace export
archive (``put_object_bytes``) and the checkpoint (``checkpoint_workspace``).
Until 2026-09-02 all three read the whole body first and entered the gate only
inside ``object_put``, so ``SANDBOX_MAX_OBJECT_QUEUE`` bounded nothing about
the spooling: with no thread cap on the server, N concurrent 64MiB uploads put
N × 64MiB on a 96Mi emptyDir, and the kubelet's answer to that is to evict the
Pod - single replica, reaper included.

Two things are pinned. Against a live Control Plane whose store call is held
open, the second upload is refused with 503 **before its body is read** (the
client has sent only headers when the answer arrives). And the manifests' tmp
volume is at least ``SANDBOX_MAX_OBJECT_QUEUE × MAX_STREAM_OBJECT_BYTES``, the
ceiling the gate now makes real.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_object_owner_partition import core  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class QueueSlotSemanticsTests(unittest.TestCase):
    """``object_queue_slot`` holds one slot for the request; ``object_slot`` inside it takes no second one."""

    def gates(self, depth: int):
        from unittest import mock
        return (
            mock.patch.object(core, "_OBJECT_QUEUE_SLOTS", threading.BoundedSemaphore(depth)),
            mock.patch.object(core, "_OBJECT_SLOTS", threading.BoundedSemaphore(depth + 8)),
        )

    def test_the_store_call_inside_a_queued_request_does_not_take_a_second_slot(self) -> None:
        queue_gate, exec_gate = self.gates(1)
        with queue_gate, exec_gate:
            with core.object_queue_slot():
                # Depth 1 and the request already holds it: object_slot must not
                # refuse its own request (that would refuse at half the depth),
                # and must not wait either.
                with core.object_slot():
                    pass
            # Released on exit: the next request gets in.
            with core.object_queue_slot():
                pass

    def test_a_second_request_is_refused_while_the_first_still_spools(self) -> None:
        # A second *request* is another thread; the marker is per thread, so this
        # is the case the depth is meant to refuse.
        queue_gate, exec_gate = self.gates(1)
        outcome: list[str] = []
        with queue_gate, exec_gate:
            with core.object_queue_slot():
                def other() -> None:
                    try:
                        with core.object_queue_slot():
                            outcome.append("admitted")
                    except core.ObjectStoreBusy:
                        outcome.append("busy")
                worker = threading.Thread(target=other)
                worker.start()
                worker.join(5)
            with core.object_queue_slot():
                pass
        self.assertEqual(outcome, ["busy"])

    def test_the_marker_is_per_thread(self) -> None:
        # Another thread does not inherit this thread's slot.
        queue_gate, exec_gate = self.gates(1)
        outcome: list[str] = []
        with queue_gate, exec_gate:
            with core.object_queue_slot():
                def other() -> None:
                    try:
                        with core.object_slot():
                            outcome.append("admitted")
                    except core.ObjectStoreBusy:
                        outcome.append("busy")
                worker = threading.Thread(target=other)
                worker.start()
                worker.join(5)
        self.assertEqual(outcome, ["busy"])

    def test_object_slot_nested_in_object_slot_still_refuses(self) -> None:
        # The re-entrancy is for object_queue_slot only; the existing contract
        # of object_slot is unchanged.
        queue_gate, exec_gate = self.gates(1)
        with queue_gate, exec_gate:
            with core.object_slot():
                with self.assertRaises(core.ObjectStoreBusy):
                    with core.object_slot():
                        pass


PROBE = textwrap.dedent(
    """
    import json
    import socket
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

    kube.KubeClient = FakeKube

    from control_plane import api
    from control_plane import core as control_plane

    HOLD = threading.Event()
    ARRIVED = threading.Event()
    PUTS = []

    class FakeStore:
        def put_object(self, **kwargs):
            PUTS.append(kwargs["Key"])
            ARRIVED.set()
            HOLD.wait(30)
            return {}

    control_plane.object_store = lambda: FakeStore()
    control_plane.consume_object_ticket = lambda claims: True
    control_plane._OBJECT_QUEUE_SLOTS = threading.BoundedSemaphore(1)
    control_plane._OBJECT_SLOTS = threading.BoundedSemaphore(1)
    control_plane.STORE.ensure_schema()

    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    base = f"http://127.0.0.1:{port}"
    results = {}

    def call(name, method, path, token, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            f"{base}{path}", method=method, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        results[name] = {"status": status, "body": json.loads(payload) if payload else None}
        return results[name]["body"]

    admin = control_plane.SANDBOX_CONTROL_PLANE_TOKEN
    locator = {"scope": "agent", "agent_id": "agent-1", "run_id": "run-1", "owner": "acme/alice"}

    def ticket(name):
        return call(name, "POST", "/v1/storage/tickets", admin,
                    {**locator, "path": f"outputs/{name}.bin", "operation": "upload", "max_bytes": 4096})["access_token"]

    try:
        first = ticket("first")
        second = ticket("second")
        # First upload: its store call blocks, so it holds the only queue slot.
        def upload_first():
            call("first_upload", "PUT", "/v1/storage/content", first, raw=b"x" * 1024,
                 headers={"Content-Type": "application/octet-stream"})
        holder = threading.Thread(target=upload_first)
        holder.start()
        results["first_reached_store"] = ARRIVED.wait(10)

        # Second upload: headers only, then wait for the answer. If the gate is
        # taken before the body is read, the 503 arrives while the client is
        # still holding the body back; if it is taken after, the server sits in
        # rfile.read() until this socket times out.
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            (
                "PUT /v1/storage/content HTTP/1.1\\r\\n"
                f"Host: 127.0.0.1:{port}\\r\\n"
                f"Authorization: Bearer {second}\\r\\n"
                "Content-Type: application/octet-stream\\r\\n"
                "Content-Length: 1024\\r\\n"
                "\\r\\n"
            ).encode()
        )
        try:
            chunks = []
            # The server closes this connection after answering (the body was
            # never read); read to EOF, bounded by the socket timeout.
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            head = b"".join(chunks).decode("latin1")
            results["second_status_before_body"] = head.split(" ")[1] if head.startswith("HTTP/") else head[:80]
            results["second_body"] = head.split("\\r\\n\\r\\n", 1)[1] if "\\r\\n\\r\\n" in head else ""
        except socket.timeout:
            results["second_status_before_body"] = "timeout"
        finally:
            sock.close()
        results["puts_while_held"] = list(PUTS)
        HOLD.set()
        holder.join(30)
        results["first_status"] = results.get("first_upload", {}).get("status")
    finally:
        HOLD.set()
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
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180,
        )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class UploadGateBeforeSpoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = run_probe()

    def test_the_first_upload_holds_the_slot_and_completes(self) -> None:
        self.assertTrue(self.results["first_reached_store"])
        self.assertEqual(self.results["first_status"], 201, self.results)

    def test_the_second_upload_is_refused_before_its_body_is_read(self) -> None:
        # "503" while the client has sent only headers. A gate placed after the
        # spool answers "timeout" here: the server is blocked in rfile.read().
        self.assertEqual(self.results["second_status_before_body"], "503", self.results)
        self.assertIn("busy", self.results["second_body"])
        # And nothing of the second request reached the store.
        self.assertEqual(
            [key.rsplit("/", 1)[-1] for key in self.results["puts_while_held"]],
            ["first.bin"],
        )


def quantity_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi|Ti|k|M|G|T)?", str(value))
    assert match, f"unparsed quantity {value!r}"
    scale = {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
             "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}[match.group(2)]
    return int(match.group(1)) * scale


class TmpVolumeCoversTheQueueTests(unittest.TestCase):
    """sizeLimit >= SANDBOX_MAX_OBJECT_QUEUE × MAX_STREAM_OBJECT_BYTES, in both manifest and chart."""

    @staticmethod
    def env_of(container: dict) -> dict[str, str]:
        return {item["name"]: str(item.get("value", "")) for item in container.get("env", [])}

    def test_the_kustomize_manifest(self) -> None:
        documents = list(yaml.safe_load_all((ROOT / "k8s/control-plane.yaml").read_text(encoding="utf-8")))
        deployment = next(d for d in documents if d and d.get("kind") == "Deployment")
        spec = deployment["spec"]["template"]["spec"]
        container = next(c for c in spec["containers"] if c["name"] in {"control-plane", "api", "sandbox-control-plane"} or True)
        env = self.env_of(container)
        queue = int(env.get("SANDBOX_MAX_OBJECT_QUEUE", "32"))
        stream = int(env.get("MAX_STREAM_OBJECT_BYTES", str(64 * 1024 * 1024)))
        tmp = next(v for v in spec["volumes"] if v["name"] == "tmp")
        limit = quantity_bytes(tmp["emptyDir"]["sizeLimit"])
        self.assertGreaterEqual(limit, queue * stream, (tmp, queue, stream))

    def test_the_helm_chart(self) -> None:
        # The chart is a template, so it is read as text: the control-plane
        # Deployment is the one that sets MAX_STREAM_OBJECT_BYTES, and its tmp
        # volume is the first one after that setting.
        text = (ROOT / "charts/sandbox/templates/resources.yaml").read_text(encoding="utf-8")
        stream_match = re.search(r"MAX_STREAM_OBJECT_BYTES\n\s+value: \"(\d+)\"", text)
        self.assertIsNotNone(stream_match, "the chart must set MAX_STREAM_OBJECT_BYTES on the control plane")
        stream = int(stream_match.group(1))
        tail = text[stream_match.end():]
        queue_match = re.search(r"SANDBOX_MAX_OBJECT_QUEUE\n\s+value: \"(\d+)\"", tail)
        queue = int(queue_match.group(1)) if queue_match else 32
        limit = re.search(r"sizeLimit: (\S+)\n\s+name: tmp", tail)
        self.assertIsNotNone(limit, "the control-plane tmp volume must carry a sizeLimit")
        self.assertGreaterEqual(quantity_bytes(limit.group(1)), queue * stream)


if __name__ == "__main__":
    unittest.main()
