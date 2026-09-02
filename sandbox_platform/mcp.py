"""MCP server exposing the sandbox Shell MCP over stdio.

Why this exists: the Runtime Shell MCP only accepts short-lived scoped tokens
issued by the Control Plane, and additionally requires the Mcp-Method / Mcp-Name
headers and the body _meta protocol version to agree across three places — all
of which is already wrapped by the SDK (sandbox_client.SandboxManager).
This module re-adapts that client into a stdio MCP so that any local MCP host
(Claude Code, etc.) can run commands in the sandbox without holding a Control Plane
admin token. Lease caching and token renewal live in SandboxManager; this
module only does the protocol-side adaptation.

Protocol trade-off: initialize / tools/list / tools/call run over JSON-RPC 2.0
on stdin/stdout, with zero third-party dependencies.

The surface exposes shell execution, PTY sessions, status, workspace file
operations, and workspace checkpoints. The management plane (creating tenants,
issuing admin keys, and releasing arbitrary sandboxes) never reaches the agent.
Releasing one's own runtime is also left to session-lifecycle management rather
than exposed as a standalone tool.

Installed command:
    sandbox-mcp

Source-checkout command:
    python3 -m sandbox_platform.mcp

Environment variables (all three are checked at startup, before any tool call):
  SANDBOX_CONTROL_PLANE_URL   Control Plane base URL, for example http://127.0.0.1:18080
  SANDBOX_TOKEN        Control Plane token (run `make dev-token` for the local profile)
  SANDBOX_SESSION_ID   Any stable string identifying this agent session; it
                       selects the Workspace the tools operate on

"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from sandbox_platform import __version__
from sandbox_platform.sandbox_client import ControlPlaneError, SandboxManager

# Only tools/list and tools/call are used, and both tolerate unknown protocol
# versions. Echoing a concrete version is safer than guessing a newer value.
FALLBACK_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sandbox", "version": __version__}
# Checked before serving so a misconfigured host fails once, loudly, instead of
# returning a RuntimeError from every tool call.
REQUIRED_ENVIRONMENT = {
    "SANDBOX_CONTROL_PLANE_URL": "Control Plane base URL, e.g. http://127.0.0.1:18080",
    "SANDBOX_TOKEN": "Control Plane token (run `make dev-token` for the local profile)",
    "SANDBOX_SESSION_ID": (
        "any stable string that identifies this agent session; "
        "it selects the Workspace"
    ),
}

manager = SandboxManager()

TOOLS = [
    {
        "name": "shell",
        "description": (
            "Run one shell command in the gVisor sandbox and wait for completion. "
            "The persistent working directory is /workspace; output is capped at 64 KiB. "
            "Use it for one-off builds, tests, and scripts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "command to execute"},
                "timeout_seconds": {
                    "type": "integer",
                    "description": "timeout seconds, 1-30, default 30; timeout kills the process group",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "shell_session",
        "description": (
            "Operate a persistent PTY session in the sandbox with retained cwd/env. "
            "action=exec starts a session (optionally async), wait reads output, "
            "input sends input, and kill terminates it. Callers choose session_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["exec", "wait", "input", "kill"],
                },
                "session_id": {"type": "string"},
                "command": {"type": "string"},
                "input_text": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "async_mode": {"type": "boolean"},
            },
            "required": ["action", "session_id"],
        },
    },
    {
        "name": "sandbox_status",
        "description": "Show the workspace and runtime (gVisor Pod) status bound to this session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # File tools match the backend agent surface. SandboxManager owns HTTP and
    # error disclosure; this bridge only adapts schemas. glob/grep require an
    # online Runtime, while the volume role serves the other tools offline.
    {
        "name": "file_read",
        "description": (
            "Read a workspace file using a path relative to /workspace; absolute "
            "paths and .. are rejected. Returns up to 500 lines by default; "
            "offset starts at 1 and limit=0 means read to EOF."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "Write or overwrite a workspace UTF-8 text file. Directories are created automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "file_edit",
        "description": "Replace an exact literal string (not a regex). The old value must occur exactly once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "file_glob",
        "description": (
            "Match workspace file paths by pattern. Requires an online Runtime; "
            "on 409, run shell once to start a sandbox first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "starting subdirectory, defaults to the root"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "file_grep",
        "description": (
            "Search workspace file contents. Requires an online Runtime; on 409, "
            "run shell once to start a sandbox first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string", "description": "file filter, such as *.py"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "workspace_checkpoint",
        "description": (
            "Checkpoint operations: create packages the current workspace, list "
            "returns history, and restore rolls back to a checkpoint. "
            "create/restore requires an online Runtime (after offline 400/409, "
            "run shell once first); list works offline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "restore"],
                },
                "checkpoint_id": {
                    "type": "string",
                    "description": "required for restore; use a value returned by list",
                },
            },
            "required": ["action"],
        },
    },
]


def _tool_result(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def call_tool(name: str, arguments: dict) -> dict:
    if name == "shell":
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return _tool_error("command is required")
        timeout = arguments.get("timeout_seconds", 30)
        return _tool_result(manager.shell(command, timeout_seconds=int(timeout)))
    if name == "shell_session":
        action = arguments.get("action")
        session_id = arguments.get("session_id")
        if action not in {"exec", "wait", "input", "kill"} or not session_id:
            return _tool_error(
                "action (exec|wait|input|kill) and session_id are required"
            )
        kwargs: dict[str, Any] = {}
        if action == "exec":
            kwargs["command"] = arguments.get("command", "")
            kwargs["async_mode"] = bool(arguments.get("async_mode", False))
        elif action == "input":
            kwargs["input_text"] = arguments.get("input_text", "")
        result = manager.shell_session(
            action,
            session_id,
            timeout_seconds=int(arguments.get("timeout_seconds", 30)),
            **kwargs,
        )
        return _tool_result(result)
    if name == "sandbox_status":
        # status() is read-only and does not trigger runtime provisioning; mutable workspace operations have one
        # implementation only: Runtime MCP.  No sidecar or volume fallback.
        status = manager.status()
        files = "runtime_mcp" if status.get("runtime_ready") else "offline"
        return _tool_result({**status, "runtime": "gvisor", "files": files})
    if name == "file_read":
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return _tool_error("path is required")
        return _tool_result(
            manager.read_file(
                path,
                offset=int(arguments.get("offset", 1) or 1),
                limit=int(arguments.get("limit", 500) or 0),
            )
        )
    if name == "file_write":
        path, content = arguments.get("path"), arguments.get("content")
        if not isinstance(path, str) or not path or not isinstance(content, str):
            return _tool_error("path and content (string) are required")
        return _tool_result(manager.write_file(path, content))
    if name == "file_edit":
        path, old, new = (
            arguments.get("path"),
            arguments.get("old"),
            arguments.get("new"),
        )
        if not all(isinstance(v, str) and v for v in (path, old)) or not isinstance(
            new, str
        ):
            return _tool_error("path, old (non-empty) and new (string) are required")
        return _tool_result(manager.edit_file(path, old, new))
    if name == "file_glob":
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _tool_error("pattern is required")
        return _tool_result(
            manager.glob_files(pattern, path=arguments.get("path", "") or "")
        )
    if name == "file_grep":
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _tool_error("pattern is required")
        return _tool_result(
            manager.grep_files(
                pattern,
                path=arguments.get("path", "") or "",
                file_glob=arguments.get("glob", "") or "",
            )
        )
    if name == "workspace_checkpoint":
        action = arguments.get("action")
        if action == "create":
            return _tool_result(manager.checkpoint_workspace())
        if action == "list":
            return _tool_result(manager.list_workspace_checkpoints())
        if action == "restore":
            checkpoint_id = arguments.get("checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                return _tool_error("restore requires checkpoint_id")
            return _tool_result(manager.restore_workspace(checkpoint_id))
        return _tool_error("action must be create | list | restore")
    return _tool_error(f"unknown tool: {name}")


def handle(request: dict) -> dict | None:
    method = request.get("method", "")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": request.get("params", {}).get(
                    "protocolVersion", FALLBACK_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            result = call_tool(params.get("name", ""), params.get("arguments", {}))
        except ControlPlaneError as exc:
            # ControlPlaneError only has .status, message in str(exc)(RuntimeError args).
            result = _tool_error(f"control_plane error {exc.status}: {exc}")
        except (OSError, RuntimeError, ValueError) as exc:
            result = _tool_error(f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def usage() -> str:
    lines = [
        "usage: sandbox-mcp [--help | --version]",
        "",
        "stdio MCP server bridging an MCP host to a Sandbox Platform Workspace.",
        "Run it from an MCP host (for example Claude Code); it speaks JSON-RPC on",
        "stdin/stdout and has no interactive mode.",
        "",
        "Required environment variables:",
    ]
    lines += [f"  {name:<20} {hint}" for name, hint in REQUIRED_ENVIRONMENT.items()]
    lines += ["", "Tools:"]
    lines += [f"  {tool['name']}" for tool in TOOLS]
    return "\n".join(lines) + "\n"


def missing_environment(environ: dict[str, str] | None = None) -> list[str]:
    values = os.environ if environ is None else environ
    return [name for name in REQUIRED_ENVIRONMENT if not values.get(name)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(arg in {"-h", "--help"} for arg in args):
        sys.stdout.write(usage())
        return 0
    if "--version" in args:
        sys.stdout.write(f"sandbox-mcp {__version__}\n")
        return 0
    if args:
        sys.stderr.write(f"sandbox-mcp: unexpected arguments: {' '.join(args)}\n")
        sys.stderr.write(usage())
        return 2
    missing = missing_environment()
    if missing:
        sys.stderr.write(
            "sandbox-mcp: missing required environment variables:\n"
            + "".join(
                f"  {name:<20} {REQUIRED_ENVIRONMENT[name]}\n" for name in missing
            )
        )
        return 2
    if sys.stdin.isatty():
        sys.stderr.write(
            "sandbox-mcp: this is a stdio MCP server, run it from an MCP host "
            "(stdin is a terminal; see --help)\n"
        )
        return 2
    serve(sys.stdin, sys.stdout)
    return 0


def serve(stdin, stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
