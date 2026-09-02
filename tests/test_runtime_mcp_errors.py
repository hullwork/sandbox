"""Error exits and socket budgets of the Runtime MCP server (runtime/runtime_server.py).

``test_runtime_exec.py`` calls the exec functions in-process, which cannot show
what a client actually receives. These tests run a real ``McpHandler`` on a
kernel-assigned port and speak HTTP to it, because the properties under test
only exist on the wire:

* an exhausted session quota has to come back as a JSON-RPC error the caller
  can act on, not as a dropped connection that the proxy folds into a generic
  502 -- "all N shell sessions are busy" is the only sentence that tells the
  caller to change session_id or retry instead of rebuilding the sandbox;
* on the SSE path the headers are already out, so the error has to arrive as a
  final frame; a truncated stream is indistinguishable from a quiet command;
* the file tools and the shell share one workspace, and a path that escapes it
  is a structured error rather than a traceback;
* the handler's socket timeout really lands on the connection, drops a silent
  client, and still does not cut a long stream short.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
WORKSPACE_DIR = tempfile.TemporaryDirectory(prefix="w4-runtime-mcp-")


def load_runtime_server():
    os.environ.setdefault("SANDBOX_CAPABILITY_KEY", "test-capability-key")
    os.environ.setdefault("SANDBOX_ID", "sb-0123456789ab")
    os.environ.setdefault("WORKSPACE_ID", "ws-123456789abc")
    os.environ["SANDBOX_WORKSPACE"] = WORKSPACE_DIR.name
    os.environ["FILE_SERVICE_WORKSPACE"] = WORKSPACE_DIR.name
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "runtime"))
    path = ROOT / "runtime/runtime_server.py"
    spec = importlib.util.spec_from_file_location("runtime_server_mcp_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_server = load_runtime_server()
shell_sessions = sys.modules["shell_sessions"]

import capability_ticket  # noqa: E402


def runtime_ticket() -> str:
    """Mint a runtime capability ticket the way the Control Plane does.

    The Runtime verifies a short-lived ticket signed with the instance key, not
    a static bearer token. A test that kept sending a static token would get a
    plain 401 with no hint that the credential shape is what changed.
    """
    return capability_ticket.issue(
        runtime_server.CAPABILITY_KEY,
        "runtime",
        runtime_server.SANDBOX_ID,
        runtime_server.CAPABILITY_EPOCH,
    )


def mcp_headers(method: str, *, stream: bool, name: str = "shell") -> dict:
    return {
        "Authorization": f"Bearer {runtime_ticket()}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": runtime_server.PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Mcp-Name": name,
        "Accept": "text/event-stream" if stream else "application/json",
    }


def tool_call(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": runtime_server.PROTOCOL_VERSION,
        },
    }


def shell_call(arguments: dict, request_id: int = 1) -> dict:
    return tool_call("shell", arguments, request_id=request_id)


def sse_frames(body: str) -> list[dict]:
    frames = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
    return frames


class QuietMcpHandler(runtime_server.McpHandler):
    """The real handler, minus one log line per request on the suite's stdout.

    Only log_message is overridden, so every attribute these tests patch on
    McpHandler -- timeout, send_json, is_stream_call -- still resolves through
    this subclass.
    """

    def log_message(self, fmt: str, *args: object) -> None:
        pass


class LocalRuntimeServer:
    """A real McpHandler in this process on a kernel-assigned port.

    Calling handle_mcp directly would not answer either question these tests
    ask -- whether an exception escaped the handler, and whether the socket
    timeout cuts a stream -- because both only exist once real HTTP is spoken.
    """

    def __init__(self) -> None:
        self.server = runtime_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), QuietMcpHandler
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="runtime-mcp-test", daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def post(
        self,
        payload: dict,
        *,
        stream: bool = False,
        timeout: float = 30,
        name: str | None = None,
    ) -> tuple[int, str]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=timeout
        )
        try:
            connection.request(
                "POST",
                "/mcp",
                body=json.dumps(payload),
                headers=mcp_headers(
                    payload["method"],
                    stream=stream,
                    name=name or (payload.get("params") or {}).get("name", "shell"),
                ),
            )
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def raw_socket(self) -> socket.socket:
        connection = socket.create_connection(("127.0.0.1", self.port))
        connection.settimeout(20)
        return connection


def session_manager(workspace: pathlib.Path, *, max_sessions: int):
    return shell_sessions.SessionManager(
        workspace,
        {
            "HOME": str(workspace),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PWD": str(workspace),
            "PS1": "",
            "PS2": "",
            "PROMPT_COMMAND": "",
            "TERM": "dumb",
        },
        max_sessions=max_sessions,
        idle_ttl_seconds=30,
        cleanup_interval_seconds=0,
        max_output_chars=64_000,
        max_timeout_seconds=5,
        max_wait_timeout_seconds=5,
    )


@unittest.skipUnless(os.path.exists(BASH), "bash is required for the shell tool")
class RuntimeWorkspaceToolTests(unittest.TestCase):
    """The file tools and the shell run in one process over one filesystem."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-runtime-tools-")
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = pathlib.Path(self.tempdir.name).resolve()
        (self.workspace / "src").mkdir()
        (self.workspace / ".sandbox").mkdir()
        files = runtime_server.workspace_files
        self.addCleanup(setattr, files, "WORKSPACE", files.WORKSPACE)
        self.addCleanup(
            setattr, runtime_server, "WORKSPACE", runtime_server.WORKSPACE
        )
        files.WORKSPACE = self.workspace
        runtime_server.WORKSPACE = self.workspace
        self.server = LocalRuntimeServer()
        self.addCleanup(self.server.close)

    def call(self, name: str, arguments: dict) -> dict:
        status, body = self.server.post(tool_call(name, arguments), name=name)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_write_read_and_shell_share_one_workspace(self) -> None:
        written = self.call(
            "file_write", {"path": "src/hello.txt", "content": "hello\n"}
        )
        self.assertEqual(
            written["result"]["structuredContent"]["path"], "src/hello.txt"
        )
        self.assertEqual(
            (self.workspace / "src/hello.txt").read_text(encoding="utf-8"),
            "hello\n",
        )
        read = self.call("file_read", {"path": "src/hello.txt"})
        self.assertEqual(
            read["result"]["structuredContent"]["content"], "hello\n"
        )
        # The shell sees the same bytes, from the same working directory.
        shell = self.call(
            "shell",
            {
                "action": "exec",
                "command": "printf '%s' \"$(cat src/hello.txt)\"",
                "timeout_seconds": 5,
            },
        )
        self.assertEqual(shell["result"]["structuredContent"]["stdout"], "hello")

    def test_a_path_escape_is_a_structured_mcp_error(self) -> None:
        payload = self.call(
            "file_write", {"path": "../escape.txt", "content": "no"}
        )
        self.assertEqual(payload["error"]["code"], -32602)
        self.assertIn("escapes /workspace", payload["error"]["message"])
        self.assertFalse((self.workspace.parent / "escape.txt").exists())


@unittest.skipUnless(os.path.exists(BASH), "bash is required for the shell tool")
class RuntimeErrorExitTests(unittest.TestCase):
    """Anything the tool layer raises must come back as a readable answer.

    The handler used to catch only ValueError, so SessionCapacityError and the
    three RuntimeErrors raised while writing to a PTY escaped it: the client
    saw RemoteDisconnected and the proxy turned that into a generic 502.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-runtime-errors-")
        self.addCleanup(self.tempdir.cleanup)
        self.manager = session_manager(
            pathlib.Path(self.tempdir.name), max_sessions=1
        )
        self.addCleanup(self.manager.close)
        patcher = mock.patch.object(
            runtime_server, "SESSION_MANAGER", self.manager
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LocalRuntimeServer()
        self.addCleanup(self.server.close)

    def occupy_the_only_slot(self) -> None:
        status, body = self.server.post(
            shell_call(
                {
                    "action": "session_exec",
                    "session_id": "busy",
                    "command": "sleep 30",
                    "async": True,
                    "timeout_seconds": 2,
                }
            )
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertNotIn("error", payload, body)
        self.assertTrue(payload["result"]["structuredContent"]["running"])

    def test_capacity_exhaustion_answers_with_a_json_rpc_error(self) -> None:
        self.occupy_the_only_slot()
        status, body = self.server.post(
            shell_call(
                {
                    "action": "session_exec",
                    "session_id": "newcomer",
                    "command": "echo hi",
                    "timeout_seconds": 2,
                },
                request_id=7,
            )
        )
        # The point is that this is an answer at all, not a RemoteDisconnected.
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["id"], 7)
        self.assertEqual(
            payload["error"]["code"], runtime_server.SESSION_BUSY_CODE
        )
        self.assertIn("shell sessions are busy", payload["error"]["message"])

    def test_capacity_exhaustion_on_the_sse_path_sends_a_final_error_frame(
        self,
    ) -> None:
        """The SSE headers are already out, so a raise here truncates the stream.

        To a client a truncated stream looks exactly like a command that has
        not printed anything yet.
        """
        self.occupy_the_only_slot()
        status, body = self.server.post(
            shell_call(
                {
                    "action": "session_exec",
                    "session_id": "newcomer",
                    "command": "echo hi",
                    "timeout_seconds": 2,
                },
                request_id=9,
            ),
            stream=True,
        )
        self.assertEqual(status, 200)
        frames = sse_frames(body)
        self.assertTrue(frames, f"zero frames means the stream was cut: {body!r}")
        last = frames[-1]
        self.assertEqual(last["id"], 9)
        self.assertEqual(last["error"]["code"], runtime_server.SESSION_BUSY_CODE)
        self.assertIn("shell sessions are busy", last["error"]["message"])

    def test_a_write_failure_becomes_an_internal_error_not_a_reset(self) -> None:
        """The RuntimeErrors from writing to the PTY take the same exit.

        In production they fire when the terminal's line buffer stays full for
        WRITE_TIMEOUT_SECONDS. Raising the same exception from the execution
        layer keeps the subject the handler's exit rather than the PTY.
        """
        with mock.patch.object(
            runtime_server,
            "execute_session_shell",
            side_effect=RuntimeError(
                "write to shell session timed out with 512 bytes left"
            ),
        ):
            status, body = self.server.post(
                shell_call(
                    {
                        "action": "session_exec",
                        "session_id": "stuck",
                        "command": "echo hi",
                        "timeout_seconds": 2,
                    },
                    request_id=11,
                )
            )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            payload["error"]["code"], runtime_server.INTERNAL_ERROR_CODE
        )
        # The byte count is the only thing that makes this diagnosable.
        self.assertIn("512 bytes left", payload["error"]["message"])

    def test_an_unexpected_exception_still_produces_a_readable_answer(self) -> None:
        """do_POST's backstop: nothing escaping the handler may reset the connection."""
        with mock.patch.object(
            runtime_server.McpHandler,
            "is_stream_call",
            side_effect=KeyError("something nobody predicted"),
        ):
            status, body = self.server.post(
                shell_call({"action": "exec", "command": "echo hi"})
            )
        self.assertEqual(status, 500)
        payload = json.loads(body)
        self.assertEqual(
            payload["error"]["code"], runtime_server.INTERNAL_ERROR_CODE
        )


class RuntimeSocketTimeoutDefaultTests(unittest.TestCase):
    """The production value has to land on a real connection.

    RuntimeSocketTimeoutTests below patches the timeout down to run fast, which
    makes it blind to the value in the source: replace
    ``timeout = REQUEST_SOCKET_TIMEOUT_SECONDS`` with ``timeout = None`` and
    those three stay green. So this criterion reads the socket the handler is
    actually holding, on an unpatched server.
    """

    def test_the_connection_really_carries_the_socket_timeout(self) -> None:
        observed: list[float | None] = []
        real_send_json = runtime_server.McpHandler.send_json

        def spy(handler, status, payload):
            # socketserver calls settimeout only when the class attribute is
            # not None, so the socket is where the answer is.
            observed.append(handler.connection.gettimeout())
            return real_send_json(handler, status, payload)

        server = LocalRuntimeServer()
        self.addCleanup(server.close)
        with mock.patch.object(runtime_server.McpHandler, "send_json", spy):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.port, timeout=10
            )
            try:
                connection.request("GET", "/healthz")
                self.assertEqual(connection.getresponse().status, 200)
            finally:
                connection.close()

        self.assertTrue(observed, "no request was observed")
        self.assertIsNotNone(
            observed[0],
            "the connection carries no socket timeout: readline() on the "
            "request line and rfile.read(length) on the body would both block "
            "forever, so one bare connection can pin a thread",
        )
        # The lower bound is not arbitrary: well above a normal request, or the
        # server starts killing slow clients. The upper bound is how long a
        # thread may stay hostage.
        self.assertGreaterEqual(observed[0], 5.0)
        self.assertLessEqual(observed[0], 120.0)


@unittest.skipUnless(os.path.exists(BASH), "bash is required for the shell tool")
class RuntimeSocketTimeoutTests(unittest.TestCase):
    """Two ways to pin a thread are closed, and a long stream is not.

    Reachability does not need another Pod: the sandbox's own shell reaches
    127.0.0.1 in the same netns, which no NetworkPolicy covers.

    The timeout is patched to one second here purely for speed, which leaves
    this class insensitive to the value in the source; that value is guarded by
    RuntimeSocketTimeoutDefaultTests above.
    """

    TIMEOUT = 1.0

    def setUp(self) -> None:
        patcher = mock.patch.object(
            runtime_server.McpHandler, "timeout", self.TIMEOUT
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LocalRuntimeServer()
        self.addCleanup(self.server.close)

    def test_a_connection_that_never_speaks_is_dropped(self) -> None:
        connection = self.server.raw_socket()
        self.addCleanup(connection.close)
        began = time.monotonic()
        try:
            leftover = connection.recv(1)
        except TimeoutError as exc:
            self.fail(f"the server never closed the connection; thread pinned: {exc}")
        elapsed = time.monotonic() - began
        self.assertEqual(
            leftover, b"", "the server should close the connection, not answer"
        )
        self.assertLess(
            elapsed,
            self.TIMEOUT + 10,
            "closing took far longer than the socket timeout",
        )

    def test_a_lying_content_length_does_not_pin_the_thread(self) -> None:
        connection = self.server.raw_socket()
        self.addCleanup(connection.close)
        connection.sendall(
            b"POST /mcp HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            + f"Authorization: Bearer {runtime_ticket()}\r\n".encode("utf-8")
            + b"Content-Type: application/json\r\n"
            b"Content-Length: 5000\r\n"
            b"\r\n"
            b'{"a": 1}'  # eight bytes given, 4992 that never arrive
        )
        try:
            while connection.recv(4096):
                pass
        except TimeoutError as exc:
            self.fail(f"reading the body blocked forever; thread pinned: {exc}")
        # The thread came back: the server still takes work.
        status, _ = self.server.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion":
                        runtime_server.PROTOCOL_VERSION,
                },
            }
        )
        self.assertEqual(status, 200)

    def test_a_long_stream_outlives_the_socket_timeout(self) -> None:
        """The reverse guard: the timeout must not cut a healthy long stream.

        settimeout only fires on real socket operations, and the streaming path
        waits on the child's pipes, not the socket. A one-second socket timeout
        and a three-second silent command would collide if that were wrong --
        and the same server does still drop a silent connection, which is the
        assertion at the end, so "the timeout never took effect" cannot pass
        for a green result here.
        """
        status, body = self.server.post(
            shell_call(
                {
                    "action": "exec_stream",
                    "command": "sleep 3; echo done",
                    "timeout_seconds": 10,
                }
            ),
            stream=True,
            timeout=30,
        )
        self.assertEqual(status, 200)
        frames = sse_frames(body)
        self.assertTrue(frames, f"the stream was cut off by the timeout: {body!r}")
        result = frames[-1]["result"]["structuredContent"]
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("done", result["stdout"])
        self.assertFalse(result["timed_out"])

        idle = self.server.raw_socket()
        self.addCleanup(idle.close)
        self.assertEqual(
            idle.recv(1),
            b"",
            "the timeout never took effect, so the assertion above tested nothing",
        )


if __name__ == "__main__":
    unittest.main()
