#!/usr/bin/env python3
"""Adversarial file, Runtime, and object-store checks against Control Plane."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


BASE_URL = os.environ.get(
    "SANDBOX_CONTROL_PLANE_URL", "http://127.0.0.1:18080"
).rstrip("/")
KUBE_CONTEXT = os.environ.get(
    "SANDBOX_KUBE_CONTEXT", "sandbox-local"
)
PROTOCOL_VERSION = "2026-07-28"
OBJECT_OWNER = os.environ.get("OBJECT_OWNER", "local/local")


def secret_value(key: str) -> bytes:
    encoded = subprocess.check_output([
        "kubectl",
        "--context",
        KUBE_CONTEXT,
        "--namespace",
        "sandbox-system",
        "get",
        "secret",
        "sandbox-api-credentials",
        f"--output=jsonpath={{.data.{key}}}",
    ])
    return base64.b64decode(encoded)


SANDBOX_CONTROL_PLANE_TOKEN = secret_value("control-plane-token").decode("utf-8")
SIGNING_KEY = secret_value("signing-key")


def api(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> tuple[int, Any]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    body = None
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        decoded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        decoded = raw
    return status, decoded


def expect(status: int, expected: int, body: Any) -> None:
    assert status == expected, (status, expected, body)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def scoped_token(kind: str, subject: str, expires_at: int) -> str:
    claims = {
        "aud": "sandbox-control-plane",
        "kind": kind,
        "sub": subject,
        "exp": expires_at,
    }
    payload = b64url(
        json.dumps(
            claims, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
    signature = b64url(
        hmac.new(SIGNING_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{payload}.{signature}"


def mcp_call(sandbox_id: str, token: str, command: str, timeout: int) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": f"adv-{uuid.uuid4().hex[:8]}",
        "method": "tools/call",
        "params": {
            "name": "shell",
            "arguments": {
                "action": "exec",
                "command": command,
                "timeout_seconds": timeout,
            },
        },
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/client": {
                "name": "sandbox-adversarial-e2e",
                "version": "1.0",
            },
        },
    }
    status, body = api(
        "POST",
        f"/v1/sandboxes/{sandbox_id}/mcp",
        token=token,
        payload=payload,
        extra_headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "shell",
        },
        timeout=timeout + 30,
    )
    expect(status, 200, body)
    assert "error" not in body, body
    return body["result"]["structuredContent"]


def mcp_session_call(
    sandbox_id: str,
    token: str,
    arguments: dict[str, Any],
    timeout: int,
) -> dict:
    request_id = f"session-{uuid.uuid4().hex[:8]}"
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "shell", "arguments": arguments},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/client": {
                "name": "sandbox-session-e2e",
                "version": "1.0",
            },
        },
    }
    status, body = api(
        "POST",
        f"/v1/sandboxes/{sandbox_id}/mcp",
        token=token,
        payload=payload,
        extra_headers={
            "Accept": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "shell",
        },
        timeout=timeout + 30,
    )
    expect(status, 200, body)
    assert "error" not in body, body
    return body["result"]["structuredContent"]


def mcp_stream(
    sandbox_id: str,
    token: str,
    command: str,
    timeout: int,
    *,
    close_after_chunks: int | None = None,
) -> dict[str, Any]:
    request_id = f"stream-{uuid.uuid4().hex[:8]}"
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "shell",
            "arguments": {
                "action": "exec_stream",
                "command": command,
                "timeout_seconds": timeout,
            },
        },
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/client": {
                "name": "sandbox-stream-e2e",
                "version": "1.0",
            },
        },
    }
    parsed = urllib.parse.urlsplit(BASE_URL)
    connection = http.client.HTTPConnection(
        parsed.hostname,
        parsed.port or 80,
        timeout=timeout + 30,
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection.request(
        "POST",
        f"/v1/sandboxes/{sandbox_id}/mcp",
        body=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "shell",
            "Connection": "close",
        },
    )
    response = connection.getresponse()
    assert response.status == 200, (response.status, response.read())
    assert "text/event-stream" in response.getheader("Content-Type", "")
    started = time.monotonic()
    notifications: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    data_lines: list[str] = []
    try:
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line or not data_lines:
                continue
            message = json.loads("\n".join(data_lines))
            data_lines.clear()
            if message.get("method") == "notifications/progress":
                notifications.append(
                    {
                        **message,
                        "received_seconds": round(
                            time.monotonic() - started, 3
                        ),
                    }
                )
                if (
                    close_after_chunks is not None
                    and len(notifications) >= close_after_chunks
                ):
                    connection.close()
                    return {
                        "notifications": notifications,
                        "final": None,
                        "duration_seconds": round(
                            time.monotonic() - started, 3
                        ),
                    }
            elif message.get("id") == request_id:
                final = message
                break
    finally:
        connection.close()
    return {
        "notifications": notifications,
        "final": final,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    workspace_id = ""
    sandbox_id = ""
    upload_id = f"adv-{suffix}"
    object_paths: list[str] = []
    results: dict[str, Any] = {}
    try:
        status, health = api("GET", "/healthz")
        expect(status, 200, health)
        assert health.get("object_storage") == "ok", health

        status, workspace = api(
            "POST",
            "/v1/workspaces",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            payload={"session_id": f"adversarial-{suffix}"},
        )
        expect(status, 201, workspace)
        workspace_id = workspace["workspace_id"]
        workspace_token = workspace["access_token"]

        #🔴 Runtime must be started before any file operations. File Service inline into Runtime
        #After that, write operations such as files/write-binary will fall on the File Service of Runtime.
        #When there is no runtime, Control Plane directly returns 409:
        #   {"error": "write-binary requires a running Runtime for ws-...",
        #    "hint": "create a sandbox for this workspace first"}
        #It turns out that the code for building the sandbox is ranked after nearly 150 lines, and all the previous file use cases cannot pass it.
        status, sandbox = api(
            "POST",
            "/v1/sandboxes",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            payload={"workspace_id": workspace_id},
        )
        expect(status, 201, sandbox)
        sandbox_id = sandbox["id"]
        sandbox_token = sandbox["access_token"]


        expired = scoped_token(
            "workspace", workspace_id, int(time.time()) - 10
        )
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/list",
            token=expired,
            query={"path": ""},
        )
        expect(status, 401, body)
        tampered = workspace_token[:-1] + (
            "A" if workspace_token[-1] != "A" else "B"
        )
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/list",
            token=tampered,
            query={"path": ""},
        )
        expect(status, 401, body)
        results["scoped_tokens"] = {"expired": 401, "tampered": 401}

        unicode_path = "src/中文 空格/emoji-ß.txt"
        unicode_content = "第一行\nemoji: 🧪🚀\nnull-name-is-text: \\u0000\n"
        status, body = api(
            "POST",
            f"/v1/workspaces/{workspace_id}/files/write",
            token=workspace_token,
            payload={"path": unicode_path, "content": unicode_content},
        )
        expect(status, 200, body)
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": unicode_path},
        )
        expect(status, 200, body)
        assert body["content"] == unicode_content, body

        binary = bytes(range(256)) * 16
        binary_digest = hashlib.sha256(binary).hexdigest()
        status, body = api(
            "POST",
            f"/v1/workspaces/{workspace_id}/files/write-binary",
            token=workspace_token,
            payload={
                "path": "artifacts/all-bytes.bin",
                "content_base64": base64.b64encode(binary).decode("ascii"),
                "sha256": binary_digest,
            },
        )
        expect(status, 200, body)
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read-binary",
            token=workspace_token,
            query={"path": "artifacts/all-bytes.bin"},
        )
        expect(status, 200, body)
        assert body["sha256"] == binary_digest
        assert base64.b64decode(body["content_base64"]) == binary
        results["file_encoding"] = {
            "unicode": True,
            "binary_bytes": len(binary),
        }

        invalid_writes = [
            ("write", {"path": "../escape", "content": "x"}),
            ("write", {"path": "/etc/passwd", "content": "x"}),
            ("write", {"path": ".sandbox/secret", "content": "x"}),
            (
                "write-binary",
                {"path": "artifacts/bad.bin", "content_base64": "***"},
            ),
            (
                "write-binary",
                {
                    "path": "artifacts/bad-digest.bin",
                    "content_base64": "eA==",
                    "sha256": "0" * 64,
                },
            ),
        ]
        for operation, payload in invalid_writes:
            status, body = api(
                "POST",
                f"/v1/workspaces/{workspace_id}/files/{operation}",
                token=workspace_token,
                payload=payload,
            )
            expect(status, 400, body)
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": "../escape"},
        )
        expect(status, 400, body)

        contents = {
            index: f"writer-{index}:" + chr(65 + index) * 50_000
            for index in range(8)
        }

        def concurrent_write(index: int) -> tuple[int, Any]:
            return api(
                "POST",
                f"/v1/workspaces/{workspace_id}/files/write",
                token=workspace_token,
                payload={
                    "path": "artifacts/concurrent.txt",
                    "content": contents[index],
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            writes = list(executor.map(concurrent_write, range(8)))
        assert all(status == 200 for status, _ in writes), writes
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": "artifacts/concurrent.txt"},
        )
        expect(status, 200, body)
        final_content = body["content"]
        # Text reads are deliberately capped at 8k, so verify one complete
        # writer prefix and the on-disk length from inside Runtime below.
        matching = [
            index
            for index, content in contents.items()
            if content.startswith(final_content)
        ]
        assert len(matching) == 1, matching
        winning_writer = matching[0]

        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/list",
            token=sandbox_token,
            query={"path": ""},
        )
        expect(status, 401, body)

        shell = mcp_call(
            sandbox_id,
            sandbox_token,
            (
                "set -eu; "
                "ln -s /etc/passwd src/outside-link; "
                "wc -c < artifacts/concurrent.txt"
            ),
            10,
        )
        assert shell["exit_code"] == 0, shell
        assert int(shell["stdout"].strip()) == len(contents[winning_writer]), shell
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": "src/outside-link"},
        )
        expect(status, 400, body)

        # Rejecting the symlink on read is not enough: search walks the tree
        # itself, so it must drop the escaping link rather than list it or
        # grep through it into /etc/passwd.
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/glob",
            token=workspace_token,
            query={"path": "", "pattern": "outside-link"},
        )
        expect(status, 200, body)
        assert body["matches"] == [], body
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/grep",
            token=workspace_token,
            query={"path": "", "pattern": "root:x:0", "mode": "files_with_matches"},
        )
        expect(status, 200, body)
        assert body["files"] == [], body
        for bad_pattern in ("../*", "/etc/*", "src/../../*"):
            status, body = api(
                "GET",
                f"/v1/workspaces/{workspace_id}/files/glob",
                token=workspace_token,
                query={"path": "", "pattern": bad_pattern},
            )
            expect(status, 400, body)
        # Paging must reach the end of a file far larger than one read window,
        # which is exactly what the pre-paging implementation could not do:
        # it always returned the first 8000 characters and offset addressed
        # that truncated prefix, so later lines were unreachable.
        paged = mcp_call(
            sandbox_id,
            sandbox_token,
            "seq 1 5000 > artifacts/paged.txt",
            10,
        )
        assert paged["exit_code"] == 0, paged
        windows, offset, last_line = 0, 1, ""
        while windows < 50:
            status, body = api(
                "GET",
                f"/v1/workspaces/{workspace_id}/files/read",
                token=workspace_token,
                query={"path": "artifacts/paged.txt", "offset": str(offset)},
            )
            expect(status, 200, body)
            windows += 1
            last_line = body["content"].rstrip("\n").rsplit("\n", 1)[-1]
            if not body.get("truncated"):
                break
            offset = body["next_offset"]
        assert windows > 1, windows
        assert last_line == "5000", (last_line, windows)
        results["file_search"] = {
            "symlink_escape_hidden_from_glob": True,
            "symlink_escape_hidden_from_grep": True,
            "traversal_patterns_rejected": 3,
            "paged_read_windows": windows,
        }

        timed = mcp_call(
            sandbox_id,
            sandbox_token,
            "(sleep 3; printf late > artifacts/late.txt) & wait",
            1,
        )
        assert timed["timed_out"] is True, timed
        assert timed["exit_code"] != 0, timed
        time.sleep(3.5)
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": "artifacts/late.txt"},
        )
        expect(status, 400, body)
        alive = mcp_call(
            sandbox_id, sandbox_token, "printf runtime-alive", 5
        )
        assert alive["exit_code"] == 0
        assert alive["stdout"] == "runtime-alive"

        session_id = f"pty-{suffix}"
        state = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_exec",
                "session_id": session_id,
                "command": (
                    "mkdir -p /workspace/pty-state && "
                    "cd /workspace/pty-state && export PTY_E2E=kept"
                ),
                "timeout_seconds": 5,
            },
            5,
        )
        assert state["exit_code"] == 0 and state["running"] is False, state
        state = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_exec",
                "session_id": session_id,
                "command": "printf '%s:%s' \"$PWD\" \"$PTY_E2E\"",
                "timeout_seconds": 5,
            },
            5,
        )
        assert state["stdout"].endswith("/workspace/pty-state:kept"), state
        interactive = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_exec",
                "session_id": session_id,
                "command": "read value; printf 'got:%s' \"$value\"",
                "timeout_seconds": 5,
                "async": True,
            },
            5,
        )
        assert interactive["running"] is True, interactive
        interactive = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_input",
                "session_id": session_id,
                "input": "from-e2e",
                "append_newline": True,
                "timeout_seconds": 5,
            },
            5,
        )
        assert interactive["exit_code"] == 0, interactive
        assert "got:from-e2e" in interactive["stdout"], interactive
        long_session = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_exec",
                "session_id": session_id,
                "command": "sleep 30",
                "timeout_seconds": 5,
                "async": True,
            },
            5,
        )
        assert long_session["running"] is True, long_session
        waited = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_wait",
                "session_id": session_id,
                "timeout_seconds": 1,
            },
            1,
        )
        assert waited["running"] and waited["wait_timed_out"], waited
        killed = mcp_session_call(
            sandbox_id,
            sandbox_token,
            {
                "action": "session_kill",
                "session_id": session_id,
                "timeout_seconds": 5,
            },
            5,
        )
        assert killed["session_restarted"] and not killed["running"], killed

        streamed = mcp_stream(
            sandbox_id,
            sandbox_token,
            (
                "printf 'stdout-1\\n'; sleep 0.4; "
                "printf 'stderr-1\\n' >&2; sleep 0.4; "
                "printf 'stdout-2\\n'"
            ),
            5,
        )
        assert streamed["final"] is not None, streamed
        notifications = streamed["notifications"]
        assert len(notifications) >= 3, notifications
        channels = [
            item["params"]["_meta"]["channel"] for item in notifications
        ]
        assert "stdout" in channels and "stderr" in channels, channels
        sequences = [
            item["params"]["_meta"]["sequence"] for item in notifications
        ]
        assert sequences == list(range(1, len(sequences) + 1)), sequences
        final_stream = streamed["final"]["result"]["structuredContent"]
        assert final_stream["exit_code"] == 0, final_stream
        assert final_stream["stdout"] == "stdout-1\nstdout-2\n", final_stream
        assert final_stream["stderr"] == "stderr-1\n", final_stream
        assert final_stream["stream_chunks"] == len(notifications)
        first_chunk_seconds = notifications[0]["received_seconds"]
        assert (
            streamed["duration_seconds"] - first_chunk_seconds >= 0.5
        ), streamed

        disconnected = mcp_stream(
            sandbox_id,
            sandbox_token,
            (
                "printf 'disconnect-1\\n'; sleep 0.4; "
                "printf 'disconnect-2\\n'; sleep 1; "
                "touch artifacts/stream-disconnect-late.txt"
            ),
            5,
            close_after_chunks=1,
        )
        assert len(disconnected["notifications"]) == 1, disconnected
        time.sleep(1.6)
        status, body = api(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/read",
            token=workspace_token,
            query={"path": "artifacts/stream-disconnect-late.txt"},
        )
        expect(status, 400, body)
        results["runtime"] = {
            "gvisor_timeout": True,
            "process_group_killed": True,
            "recovered": True,
            "symlink_escape_status": 400,
        }
        results["runtime_stream"] = {
            "chunks": len(notifications),
            "channels": sorted(set(channels)),
            "first_chunk_seconds": first_chunk_seconds,
            "duration_seconds": streamed["duration_seconds"],
            "disconnect_killed_process_group": True,
        }
        results["runtime_session"] = {
            "state_persisted": True,
            "interactive_input": True,
            "wait_preserved_process": True,
            "kill_restarted_session": True,
        }

        unicode_object = "source/中文 空格/对象.json"
        object_paths.append(unicode_object)
        object_data = "MinIO 中文对象 🪣\n".encode("utf-8")
        status, body = api(
            "POST",
            "/v1/storage/objects",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            payload={
                "scope": "upload",
                "owner": OBJECT_OWNER,
                "upload_id": upload_id,
                "path": unicode_object,
                "content_base64": base64.b64encode(object_data).decode("ascii"),
            },
        )
        expect(status, 201, body)
        status, body = api(
            "GET",
            "/v1/storage/objects",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            query={
                "scope": "upload",
                "owner": OBJECT_OWNER,
                "upload_id": upload_id,
                "path": unicode_object,
            },
        )
        expect(status, 200, body)
        assert base64.b64decode(body["content_base64"]) == object_data

        concurrent_objects = [f"source/concurrent/object-{index}.txt" for index in range(8)]
        object_paths.extend(concurrent_objects)

        def put_minio(path: str) -> tuple[int, Any]:
            return api(
                "POST",
                "/v1/storage/objects",
                token=SANDBOX_CONTROL_PLANE_TOKEN,
                payload={
                    "scope": "upload",
                    "owner": OBJECT_OWNER,
                    "upload_id": upload_id,
                    "path": path,
                    "content_base64": base64.b64encode(path.encode()).decode(),
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            minio_writes = list(executor.map(put_minio, concurrent_objects))
        assert all(status == 201 for status, _ in minio_writes), minio_writes
        status, body = api(
            "GET",
            "/v1/storage/objects/list",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            query={
                "scope": "upload",
                "owner": OBJECT_OWNER,
                "upload_id": upload_id,
                "path": "source/concurrent",
            },
        )
        expect(status, 200, body)
        assert len(body["objects"]) == len(concurrent_objects), body

        for invalid_path in ("../escape", "/absolute", "meta/../escape"):
            status, body = api(
                "POST",
                "/v1/storage/objects",
                token=SANDBOX_CONTROL_PLANE_TOKEN,
                payload={
                    "scope": "upload",
                    "owner": OBJECT_OWNER,
                    "upload_id": upload_id,
                    "path": invalid_path,
                    "content_base64": "eA==",
                },
            )
            expect(status, 400, body)

        # The owner is the whole tenant partition, so a key must not be
        # reachable with a missing, over-long or dot-segmented one.
        invalid_owners = (
            "",
            "local",
            "a/b/c",
            "../..",
            "a/..",
            ".hidden/x",
            "x" * 129 + "/y",
        )
        for invalid_owner in invalid_owners:
            status, body = api(
                "POST",
                "/v1/storage/objects",
                token=SANDBOX_CONTROL_PLANE_TOKEN,
                payload={
                    "scope": "upload",
                    "owner": invalid_owner,
                    "upload_id": upload_id,
                    "path": "source/owner-probe.txt",
                    "content_base64": "eA==",
                },
            )
            expect(status, 400, body)

        status, ticket = api(
            "POST",
            "/v1/storage/tickets",
            token=SANDBOX_CONTROL_PLANE_TOKEN,
            payload={
                "operation": "download",
                "scope": "upload",
                "owner": OBJECT_OWNER,
                "upload_id": upload_id,
                "path": unicode_object,
                "max_bytes": 1024,
            },
        )
        expect(status, 201, ticket)
        # A download ticket must not authorize the upload operation.
        status, body = api(
            "PUT",
            "/v1/storage/content",
            token=ticket["access_token"],
        )
        expect(status, 401, body)
        results["minio"] = {
            "unicode_object": True,
            "concurrent_objects": len(concurrent_objects),
            "operation_bound_ticket": 401,
            "traversal_cases": 3,
            "invalid_owner_cases": len(invalid_owners),
        }

    finally:
        if sandbox_id:
            api(
                "DELETE",
                f"/v1/sandboxes/{sandbox_id}",
                token=SANDBOX_CONTROL_PLANE_TOKEN,
            )
        if workspace_id:
            # Runtime deletion is asynchronous at the Kubernetes API. Retry
            # the workspace release until the active-runtime guard clears.
            for _ in range(50):
                status, _ = api(
                    "DELETE",
                    f"/v1/workspaces/{workspace_id}",
                    token=SANDBOX_CONTROL_PLANE_TOKEN,
                    query={"purge": "true"},
                )
                if status in {200, 404}:
                    break
                time.sleep(0.2)
        for path in object_paths:
            api(
                "DELETE",
                "/v1/storage/objects",
                token=SANDBOX_CONTROL_PLANE_TOKEN,
                query={
                    "scope": "upload",
                    "owner": OBJECT_OWNER,
                    "upload_id": upload_id,
                    "path": path,
                    "purge_versions": "true",
                },
            )

    print(json.dumps({
        "status": "passed",
        "base_url": BASE_URL,
        **results,
        "resources_cleaned": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
