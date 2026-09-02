#!/usr/bin/env python3
"""Stateless Streamable HTTP MCP server for sandboxed process and file tools."""

from __future__ import annotations

import codecs
import hashlib
import importlib.util
import json
import os
import select
import selectors
import signal
import socket
import subprocess
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import capability_ticket
from workspace_contract import WORKSPACE_LAYOUT

try:
    from .shell_sessions import (
        SESSION_ACTIONS,
        SessionCancelled,
        SessionCapacityError,
        SessionManager,
    )
except ImportError:  # Executed as /app/runtime_server.py in the Runtime image.
    from shell_sessions import (
        SESSION_ACTIONS,
        SessionCancelled,
        SessionCapacityError,
        SessionManager,
    )


HOST = os.getenv("SANDBOX_HOST", "0.0.0.0")
PORT = int(os.getenv("SANDBOX_PORT", "8080"))
# See capability_ticket.py: this is the per-instance verification key, not a
# credential any caller sends.
CAPABILITY_KEY = os.environ["SANDBOX_CAPABILITY_KEY"]
CAPABILITY_EPOCH = int(os.getenv("SANDBOX_CAPABILITY_EPOCH", "1"))
SANDBOX_ID = os.environ["SANDBOX_ID"]
WORKSPACE_ID = os.environ["WORKSPACE_ID"]
WORKSPACE = Path(os.getenv("SANDBOX_WORKSPACE", "/workspace")).resolve()
# The compatibility file API has a narrower workspace-scoped credential, while
# MCP uses the sandbox credential.  A single Runtime process serves both.
os.environ.setdefault("WORKSPACE_CAPABILITY_KEY", CAPABILITY_KEY)
os.environ.setdefault("WORKSPACE_CAPABILITY_EPOCH", str(CAPABILITY_EPOCH))
# Compatibility for importing the historical workspace module in source/tests.
os.environ.setdefault(
    "FILE_SERVICE_CAPABILITY_KEY", os.environ["WORKSPACE_CAPABILITY_KEY"]
)
os.environ.setdefault("FILE_SERVICE_WORKSPACE", str(WORKSPACE))


def _load_workspace_files():
    try:
        import file_service as module
        return module
    except ImportError:
        # Source checkout: the canonical implementation still lives at the
        # historical path while the Runtime image copies it as file_service.
        source = Path(__file__).resolve().parents[1] / "file-service/file_service.py"
        spec = importlib.util.spec_from_file_location("workspace_files", source)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load workspace operations from {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


workspace_files = _load_workspace_files()
PROTOCOL_VERSION = "2026-07-28"
MAX_BODY_BYTES = 64_000
MAX_COMMAND_BYTES = 8_192
MAX_OUTPUT_BYTES = 64_000
MAX_EXEC_TIMEOUT_SECONDS = 30
MAX_WAIT_TIMEOUT_SECONDS = 120
# The upper limit of waiting for child processes to collect corpses after SIGKILL. The value is consistent with shell_sessions.KILL_REAP_SECONDS:
# The KILLed process will only be delayed for so long if it is stuck in the uninterruptible kernel state, and the thread must be returned if it cannot wait.
KILL_REAP_SECONDS = 10.0
# BaseHTTPRequestHandler's socket timeout. If this class attribute is not set, socketserver will not adjust
# settimeout, so readline() of the request line and rfile.read(length) of the body can be permanently
# Blocking - 20 naked connections + 20 POSTs that falsely report Content-Length will kill 42 threads.
# NetworkPolicy can block cross-pods, but the sandbox's own shell goes to 127.0.0.1 in the same netns
# The sidecar and loopback are not managed by NetworkPolicy, this path is open.
#
# Why long SSE streams are not accidentally killed: settimeout only works on real socket operations (recv/send).
# The streaming path is blocked on the selector of the child process pipeline during this period, and no socket operation is performed; only
# The write of send_sse will hit the socket, and it will only block until timeout when the client does not read for 30 seconds - then the judgment
# "The client is gone" is correct. It is equally safe for session_wait to not produce any output for up to 120 seconds, because that period
# No socket operations are pending during this time.
REQUEST_SOCKET_TIMEOUT_SECONDS = 30.0
# JSON-RPC implementation defines the interval (-32000..-32099). The exhaustion of session quota is neither a parameter error (-32602) nor
# Server failure (-32603): The caller can succeed by changing the session_id or trying again later. A distinction is needed.
# code, otherwise "retry" and "don't retry" will look exactly the same to the client.
SESSION_BUSY_CODE = -32001
INTERNAL_ERROR_CODE = -32603
MAX_SESSIONS = int(os.getenv("SANDBOX_MAX_SHELL_SESSIONS", "16"))
SESSION_IDLE_TTL_SECONDS = int(
    os.getenv("SANDBOX_SHELL_SESSION_IDLE_TTL_SECONDS", "1800")
)
SESSION_MAX_WALL_SECONDS = int(
    os.getenv("SANDBOX_SHELL_SESSION_MAX_WALL_SECONDS", "3600")
)
if MAX_SESSIONS <= 0:
    raise ValueError("SANDBOX_MAX_SHELL_SESSIONS must be greater than zero")
if SESSION_IDLE_TTL_SECONDS <= 0:
    raise ValueError(
        "SANDBOX_SHELL_SESSION_IDLE_TTL_SECONDS must be greater than zero"
    )
if SESSION_MAX_WALL_SECONDS <= 0:
    raise ValueError(
        "SANDBOX_SHELL_SESSION_MAX_WALL_SECONDS must be greater than zero"
    )


class ClientDisconnected(ConnectionError):
    """The Streamable HTTP client closed before command completion."""


def connection_closed(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(
            1, socket.MSG_PEEK | socket.MSG_DONTWAIT
        ) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


SHELL_TOOL = {
    "name": "shell",
    "description": (
        "Execute a bash command inside the gVisor Runtime. "
        "The working directory is the shared /workspace. "
        "exec reports the kernel's exit status and is the path to use when the "
        "exit code matters (verifying a build, gating on tests). The session_* "
        "actions keep cwd and exported variables across calls, but their "
        "exit_code is self-reported by the shell being driven: code running "
        "inside that session can fake a successful completion. Never gate a "
        "decision on a session exit_code alone."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "exec",
                    "exec_stream",
                    "session_exec",
                    "session_wait",
                    "session_input",
                    "session_kill",
                ],
                "default": "exec",
            },
            "command": {"type": "string"},
            "session_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
            },
            "input": {"type": "string"},
            "append_newline": {"type": "boolean", "default": True},
            "async": {"type": "boolean", "default": False},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_WAIT_TIMEOUT_SECONDS,
                "default": 30,
                "description": (
                    "Per-call wait deadline. exec/exec_stream/session_exec and "
                    "session_input allow at most 30 seconds; session_wait "
                    "allows at most 120 seconds."
                ),
            },
        },
        "additionalProperties": False,
    },
}

FILE_TOOLS = [
    {
        "name": "file_read",
        "description": "Read UTF-8 text from the Runtime workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_write",
        "description": "Atomically write UTF-8 text in the Runtime workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_edit",
        "description": "Replace one exact text occurrence in a workspace file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "minLength": 1},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_glob",
        "description": "Find workspace files with a bounded shell glob.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 0},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_grep",
        "description": "Search workspace text files with bounded traversal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                },
                "case_insensitive": {"type": "boolean"},
                "context": {"type": "integer", "minimum": 0},
                "regex": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 0},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
]
FILE_TOOL_NAMES = frozenset(tool["name"] for tool in FILE_TOOLS)


def jsonrpc_result(request_id: Any, result: object) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def tool_failure(request_id: Any, exc: BaseException) -> dict:
    """Core function: Convert **any** exceptions during tool execution into a recoverable JSON-RPC error.

    Responsibilities: Only mapping exceptions to error codes; not responsible for HTTP status codes or cleaning up sessions.

    🔴 Constraints: This must be exhaustive (the last one is a bottom-up branch, not a specific type). It turns out
        The handler only catches ValueError, so there are three places in SessionCapacityError and _write
        RuntimeError escapes directly from the handler, and the client gets RemoteDisconnected;
        Control Plane's proxy_runtime_mcp is then folded into a universal one
        502 "internal service unavailable" —— "all 16 shell sessions are busy"
        This sentence has never been sent to the caller, and it is the only information that can guide the caller's behavior.
        (Change the session_id / try again later, instead of treating the sandbox as dead and rebuilding it).

    Boundary: message uses str(exc) directly. This is the internal state of the tenant's own sandbox and does not span tenants.
        What was leaked was troubleshooting information such as "write PTY timeout and N bytes left", which is worth keeping."""
    if isinstance(exc, ValueError):
        return jsonrpc_error(request_id, -32602, str(exc))
    if isinstance(exc, SessionCapacityError):
        return jsonrpc_error(request_id, SESSION_BUSY_CODE, str(exc))
    return jsonrpc_error(request_id, INTERNAL_ERROR_CODE, str(exc))


def shell_environment() -> dict[str, str]:
    return {
        "HOME": str(WORKSPACE),
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(WORKSPACE),
        "TMPDIR": "/tmp",
        "PS1": "",
        "PS2": "",
        "PROMPT_COMMAND": "",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CI": "true",
        "SANDBOX_ID": SANDBOX_ID,
        "SANDBOX_WORKSPACE_ID": WORKSPACE_ID,
    }


def shell_process_spec(arguments: dict) -> tuple[str, int, dict[str, str]]:
    action = arguments.get("action", "exec")
    if action not in {"exec", "exec_stream"}:
        raise ValueError("action must be exec or exec_stream")
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError("command must be a non-empty string")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ValueError("command is too large")
    timeout = arguments.get("timeout_seconds", MAX_EXEC_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ValueError("timeout_seconds must be an integer")
    if not 1 <= timeout <= MAX_EXEC_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_EXEC_TIMEOUT_SECONDS}"
        )

    return command, timeout, shell_environment()


SESSION_MANAGER = SessionManager(
    WORKSPACE,
    shell_environment(),
    max_sessions=MAX_SESSIONS,
    idle_ttl_seconds=SESSION_IDLE_TTL_SECONDS,
    max_wall_seconds=SESSION_MAX_WALL_SECONDS,
    max_output_chars=MAX_OUTPUT_BYTES,
    max_timeout_seconds=MAX_EXEC_TIMEOUT_SECONDS,
    max_wait_timeout_seconds=MAX_WAIT_TIMEOUT_SECONDS,
)


def shell_result(
    process: subprocess.Popen[Any],
    stdout: str,
    stderr: str,
    *,
    timed_out: bool,
) -> dict[str, Any]:
    stdout_truncated = len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
    stderr_truncated = len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES
    return {
        "exit_code": process.returncode,
        "stdout": stdout.encode("utf-8")[:MAX_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        ),
        "stderr": stderr.encode("utf-8")[:MAX_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        ),
        "timed_out": timed_out,
        "output_truncated": stdout_truncated or stderr_truncated,
        "sandbox_id": SANDBOX_ID,
        "workspace_id": WORKSPACE_ID,
    }


def execute_shell(arguments: dict) -> dict:
    """Non-streaming exec, on the same bounded reader as ``exec_stream``.

    ``communicate()`` collects everything the command prints and only then lets
    shell_result truncate it, so ``yes`` for thirty seconds is gigabytes of
    resident memory in a 256Mi Pod — the OOM kill takes every PTY session in
    that Pod with it. The streaming path already stops capturing at
    MAX_OUTPUT_BYTES; having one bounded implementation instead of two is also
    how the two paths stay consistent.
    """
    return execute_shell_stream(arguments, lambda *_args: None)


def kill_process_group(process: subprocess.Popen) -> None:
    """Core function: Kill the entire process group, not just the direct child processes.

    Responsibilities: Responsible for making "timeout" happen; not responsible for recycling, collecting corpses in reap_process.
    Constraint: The criterion **cannot** be `process.poll() is None`. `sleep 300 &` Such background processes
         Inherited the pipe writing end of bash, and bash itself has already exited - take the direct child process as the
         Living first is equivalent to never killing in the situation where killing is most needed. The process group is
         Created with start_new_session=True, the team leader is bash."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The group is empty (all exited naturally), or the PID is no longer under our control after being recycled: there is nothing to do.
        pass


def reap_process(process: subprocess.Popen) -> None:
    """Core function: Wait for the child process to collect the corpse, and never block the calling thread indefinitely.

    Constraint: Naked `process.wait()` will peg the HTTP worker thread until the command itself ends -
         `exec 1>&- 2>&-; sleep 300` turns off the two pipes to end the read loop immediately.
         The wait would then hang here and the declared timeout_seconds would not apply for a single second."""
    try:
        process.wait(timeout=KILL_REAP_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    kill_process_group(process)
    try:
        process.wait(timeout=KILL_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        # You have to return even if you can't take it away: It is cheaper to keep a zombie than to crucify a thread, and the tini of PID 1 will take advantage of it.
        pass


def execute_shell_stream(
    arguments: dict,
    on_chunk: Any,
    is_cancelled: Any | None = None,
) -> dict[str, Any]:
    command, timeout, env = shell_process_spec(arguments)
    # A failed producer must fail the whole verification pipeline. Otherwise
    # `python build.py | head` reports head's 0 even when no artifact exists.
    command = "set -o pipefail\n" + command
    # --noprofile --norc: HOME points to the writable /workspace, the login shell will source
    # $HOME/.profile, code in the sandbox can use `trap 'exit 0' EXIT` to cause everything thereafter to fail.
    # Forged to exit_code=0. PATH is injected explicitly by shell_environment(), -l is not required.
    # Maintains the same posture as shell_sessions' PTY sessions.
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=WORKSPACE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): "stdout",
        process.stderr.fileno(): "stderr",
    }
    for pipe in (process.stdout, process.stderr):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    decoders = {
        name: codecs.getincrementaldecoder("utf-8")(errors="replace")
        for name in ("stdout", "stderr")
    }
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while selector.get_map():
            if is_cancelled is not None and is_cancelled():
                raise ClientDisconnected("stream client disconnected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Just kill and leave, no longer waiting for the pipeline EOF: the background process that inherits the write side can
                # The selector is never empty, and that's where the timeout should take effect.
                timed_out = True
                kill_process_group(process)
                break
            events = selector.select(timeout=max(0.01, min(0.1, remaining)))
            for key, _ in events:
                channel = streams[key.fd]
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    final_text = decoders[channel].decode(b"", final=True)
                    if final_text:
                        on_chunk(channel, final_text)
                    continue
                if len(captured[channel]) < MAX_OUTPUT_BYTES + 1:
                    remaining_capture = MAX_OUTPUT_BYTES + 1 - len(
                        captured[channel]
                    )
                    captured[channel].extend(chunk[:remaining_capture])
                text = decoders[channel].decode(chunk)
                if text:
                    on_chunk(channel, text)
        if not timed_out:
            # Pipeline EOF does not equal end of command - `exec 1>&- 2>&-; sleep 300` would make reading
            # The loop exits immediately and the foreground process continues to run. The remaining budget here is still declared by this command
            # Timeout is not the grace period for collecting corpses. Mixing the two is equivalent to quietly enlarging the timeout to 10 seconds.
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_group(process)
        reap_process(process)
    except BaseException:
        kill_process_group(process)
        reap_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    stdout = bytes(captured["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(captured["stderr"]).decode("utf-8", errors="replace")
    return shell_result(process, stdout, stderr, timed_out=timed_out)


def execute_session_shell(
    arguments: dict[str, Any],
    on_chunk: Any,
    is_cancelled: Any | None = None,
) -> dict[str, Any]:
    result = SESSION_MANAGER.handle(arguments, on_chunk, is_cancelled)
    return {
        **result,
        "sandbox_id": SANDBOX_ID,
        "workspace_id": WORKSPACE_ID,
    }


class McpHandler(workspace_files.ApiHandler):
    server_version = "sandbox-runtime-mcp/0.4.0"
    # socketserver only calls settimeout on the connection when this class attribute is non-None - see constants
    # Explanation on "Why SSE is not accidentally killed".
    timeout = REQUEST_SOCKET_TIMEOUT_SECONDS
    # Whether this request has started writing to the socket. Determining to "make up for an error response" depends on the exception.
    # Or "you can only shut up": sending_json after the header has been sent is to insert the second copy into the response body.
    # In the message, the client sees protocol corruption, which is more difficult to detect than the original exception.
    response_started = False

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def begin_sse(self) -> None:
        self.response_started = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def send_sse(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: message\ndata: {encoded}\n\n".encode("utf-8"))
        self.wfile.flush()

    def send_sse_final_error(self, payload: dict[str, Any]) -> None:
        """Try your best to send out the last frame error; if it cannot be sent out, it will never be thrown again.

        Constraints: This is the end of the exception handling path. If it throws an exception again, it will escape from the handler.
             Just back to the hole we just plugged - the client got the truncated stream and couldn't tell whether it was
             The command hasn't been output yet or the server has crashed."""
        try:
            self.send_sse(payload)
        except OSError:
            pass
        self.close_connection = True

    def require_runtime_auth(self) -> bool:
        # The sandbox credential, distinct from the workspace one the inherited
        # file routes check: a ticket for one kind must not open the other.
        header = self.headers.get("Authorization", "")
        ticket = header[7:] if header.startswith("Bearer ") else ""
        if capability_ticket.verify(
            CAPABILITY_KEY, ticket, "runtime", SANDBOX_ID, CAPABILITY_EPOCH
        ):
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def read_request(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        return payload

    def do_GET(self) -> None:
        # The same handler instance under keep-alive will serve multiple requests in succession, and the flag must be reset to zero each time.
        self.response_started = False
        path = urlparse(self.path).path
        if path.startswith("/v1/files/"):
            super().do_GET()
            return
        if path == "/healthz":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "sandbox_id": SANDBOX_ID,
                    "workspace_id": WORKSPACE_ID,
                },
            )
            return
        if path == "/activity":
            # Function entrance: Report to the Control Plane whether this Runtime is still working.
            #
            # Responsibilities: Read only, report truthfully; not responsible for recycling decisions. Control Plane reaper in TTL
            # After expiration, adjust it to do "review before execution" - spontaneous long tasks inside the sandbox will not be generated.
            # MCP call, Control Plane cannot see expires-at alone.
            # Constraints: Same set of token verification as /mcp. It exposes running status rather than data.
            # But it still cannot be anonymous - otherwise anyone on the same network segment can enumerate the sandbox availability.
            if not self.require_runtime_auth():
                return
            self.send_json(
                HTTPStatus.OK,
                {
                    "sandbox_id": SANDBOX_ID,
                    **SESSION_MANAGER.activity_snapshot(),
                },
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        self.response_started = False
        path = urlparse(self.path).path
        if path.startswith("/v1/files/"):
            super().do_POST()
            return
        if path != "/mcp":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.require_runtime_auth():
            return
        try:
            request = self.read_request()
            if self.is_stream_call(request):
                self.handle_stream_call(request)
                return
            response = self.handle_mcp(request)
            self.send_json(HTTPStatus.OK, response)
        except ValueError as exc:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                jsonrpc_error(None, -32600, str(exc)),
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # The client left first, or read the body and hit the socket timeout (falsely reporting Content-Length).
            # There is no object to return; the connection cannot be reused - only half of the body is read, and the remaining bytes will be
            # Parsed as the request line for the next request.
            self.close_connection = True
        except Exception as exc:
            # Bottom line: If any exception escapes from the handler, the client will get RemoteDisconnected.
            # Each of the above layers has mapped out its own type of anomalies, and the only thing left at this point is the truly unexpected ones.
            # - it must also become a readable error, rather than a connection reset.
            self.close_connection = True
            if not self.response_started:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    jsonrpc_error(None, INTERNAL_ERROR_CODE, str(exc)),
                )

    def is_stream_call(self, request: dict[str, Any]) -> bool:
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        return (
            request.get("method") == "tools/call"
            and params.get("name") == "shell"
            and isinstance(arguments, dict)
            and arguments.get("action") in ({"exec_stream"} | SESSION_ACTIONS)
            and "text/event-stream" in self.headers.get("Accept", "")
        )

    def protocol_error(
        self,
        request: dict[str, Any],
        method: Any,
    ) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return jsonrpc_error(request_id, -32600, "invalid JSON-RPC version")
        if not isinstance(method, str):
            return jsonrpc_error(request_id, -32600, "method is required")
        protocol_header = self.headers.get("MCP-Protocol-Version")
        meta = request.get("_meta") or {}
        protocol_meta = meta.get("io.modelcontextprotocol/protocolVersion")
        if method not in {"initialize", "notifications/initialized"}:
            if protocol_header != PROTOCOL_VERSION:
                return jsonrpc_error(
                    request_id,
                    -32600,
                    f"MCP-Protocol-Version must be {PROTOCOL_VERSION}",
                )
            if protocol_meta != PROTOCOL_VERSION:
                return jsonrpc_error(
                    request_id,
                    -32600,
                    "protocol header and request metadata do not match",
                )
            if self.headers.get("Mcp-Method") != method:
                return jsonrpc_error(
                    request_id,
                    -32600,
                    "Mcp-Method header does not match request method",
                )
        return None

    def handle_stream_call(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        error = self.protocol_error(request, request.get("method"))
        if error is None and self.headers.get("Mcp-Name") != "shell":
            error = jsonrpc_error(
                request_id,
                -32600,
                "Mcp-Name header does not match tool name",
            )
        self.begin_sse()
        if error is not None:
            self.send_sse(error)
            return

        arguments = (request.get("params") or {}).get("arguments") or {}
        sequence = 0

        def on_chunk(channel: str, chunk: str) -> None:
            nonlocal sequence
            sequence += 1
            self.send_sse(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": request_id,
                        "progress": sequence,
                        "message": chunk,
                        "_meta": {
                            "channel": channel,
                            "sequence": sequence,
                        },
                    },
                }
            )

        try:
            action = arguments.get("action")
            cancelled = lambda: connection_closed(self.connection)
            if action == "exec_stream":
                result = execute_shell_stream(arguments, on_chunk, cancelled)
            else:
                result = execute_session_shell(arguments, on_chunk, cancelled)
            self.send_sse(
                jsonrpc_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, ensure_ascii=False),
                            }
                        ],
                        "structuredContent": {
                            **result,
                            "stream_chunks": sequence,
                        },
                        "isError": False,
                    },
                )
            )
        except (
            BrokenPipeError,
            ConnectionResetError,
            ClientDisconnected,
            SessionCancelled,
        ):
            action = arguments.get("action")
            session_id = arguments.get("session_id")
            # Only session_exec collects the session together: wait/input relies on session_id
            # Re-address the existing command. Just because the flow is cut off this time does not mean that no one wants that command - kill it.
            # Any network jitter will destroy the running build, along with cwd/environment variables.
            if action == "session_exec" and isinstance(session_id, str):
                SESSION_MANAGER.cancel(session_id)
            self.close_connection = True
        except Exception as exc:
            # The SSE header has already been sent in begin_sse, and throwing it here will cut off the stream to the client.
            # Exhausted quota (SessionCapacityError) and three RuntimeErrors of _write
            # This is the way to go. Connection exceptions must be ranked first: in that case, even "filling in a frame" cannot be done.
            # Moreover, session_exec also needs to collect the command together.
            self.send_sse_final_error(tool_failure(request_id, exc))

    def handle_mcp(self, request: dict) -> dict:
        request_id = request.get("id")
        method = request.get("method")
        error = self.protocol_error(request, method)
        if error is not None:
            return error

        if method == "server/discover":
            return jsonrpc_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": "sandbox-shell",
                        "version": "0.4.0",
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return jsonrpc_result(request_id, {"tools": [SHELL_TOOL, *FILE_TOOLS]})
        if method == "tools/call":
            params = request.get("params") or {}
            tool_name = params.get("name")
            if tool_name != "shell" and tool_name not in FILE_TOOL_NAMES:
                return jsonrpc_error(request_id, -32602, "unknown tool")
            if self.headers.get("Mcp-Name") != tool_name:
                return jsonrpc_error(
                    request_id,
                    -32600,
                    "Mcp-Name header does not match tool name",
                )
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return jsonrpc_error(
                    request_id, -32602, "arguments must be an object"
                )
            try:
                if tool_name in FILE_TOOL_NAMES:
                    result = workspace_files.execute_file_operation(
                        tool_name, arguments
                    )
                else:
                    action = arguments.get("action", "exec")
                    if action in SESSION_ACTIONS:
                        result = execute_session_shell(
                            arguments, lambda *_args: None
                        )
                    else:
                        result = execute_shell(arguments)
            except Exception as exc:
                # Non-streaming paths have no emitted headers, so any execution-time exceptions can be put in place here
                # Folding to JSON-RPC error. The catch width is intentional: the quota is exhausted, writing PTY fails,
                # Fork does not exit the process (OSError), openpty fails (termios.error) - one by one
                # The writing method of column type has missed SessionCapacityError once.
                return tool_failure(request_id, exc)
            return jsonrpc_result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ],
                    "structuredContent": result,
                    "isError": False,
                },
            )

        # Compatibility for older MCP clients during migration.
        if method == "initialize":
            return jsonrpc_result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {
                        "name": "sandbox-shell",
                        "version": "0.4.0",
                    },
                    "capabilities": {"tools": {}},
                },
            )
        if method == "notifications/initialized":
            return jsonrpc_result(request_id, {})
        return jsonrpc_error(request_id, -32601, "method not found")


if __name__ == "__main__":
    # When the log pipe is full, it is better to lose the log than to freeze the thread (see safe_stdout module and 2026-08-17
    # failure). This process is in worse shape: log_message is called before sending the response
    # (send_response -> log_request), print() Once blocked permanently, each HTTP thread stops at
    # That line, /healthz is stopped together - the probe times out and restarts, and the entire Pod's PTY session is lost.
    #
    # To clarify a common misunderstanding: this is not stdio MCP. The transport is Streamable HTTP, stdout is not carried
    # protocol, logging itself will not "ruin the MCP protocol". What needs to be cured is blockage, not pollution.
    #
    # Import is placed in __main__: install() will replace the global sys.stdout/sys.stderr, that is
    # This is what the permanent service process should do. When the tested import comes in, it cannot have this side effect.
    # safe_stdout ships inside the image as sandbox_platform/safe_stdout.py (see the Dockerfile COPY).
    from sandbox_platform import safe_stdout

    safe_stdout.install()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    for directory in WORKSPACE_LAYOUT:
        (WORKSPACE / directory).mkdir(parents=True, exist_ok=True)
    os.chmod(WORKSPACE, 0o700)
    recovery = workspace_files.recover_interrupted_restore()
    if recovery is not None:
        print(
            json.dumps({"event": "restore_recovered", **recovery}, sort_keys=True),
            flush=True,
        )
    workspace_files.record_activity()
    fingerprint = hashlib.sha256(CAPABILITY_KEY.encode("utf-8")).hexdigest()[:8]
    print(
        f"runtime MCP listening on {HOST}:{PORT}, sandbox={SANDBOX_ID}, "
        f"workspace={WORKSPACE_ID}, token fingerprint={fingerprint}",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), McpHandler).serve_forever()
