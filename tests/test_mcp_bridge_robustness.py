"""The stdio MCP bridge must answer malformed input, not exit on it.

A host sees an exited bridge as a disconnect and every lease cached in the
process is lost with it. So each case below feeds one bad line followed by a
well-formed ``tools/list`` and requires both an answer to the bad line and an
answer to the good one, from a process that exits 0 once stdin closes.

The subprocess is ``python -m sandbox_platform.mcp`` from this checkout with the
Control Plane pointed at a port nothing listens on: none of the inputs here may
reach the network, and if one did it would fail loudly as a 502 tool error,
not hang.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from sandbox_platform import mcp

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_LIST = {"jsonrpc": "2.0", "id": 99, "method": "tools/list"}


def run_bridge(*lines: str) -> tuple[int, list[dict], str]:
    env = {
        **os.environ,
        "SANDBOX_CONTROL_PLANE_URL": "http://127.0.0.1:1",
        "SANDBOX_TOKEN": "x",
        "SANDBOX_SESSION_ID": "robustness",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "sandbox_platform.mcp"],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
        check=False,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line]
    return completed.returncode, responses, completed.stderr


def call(name: str, arguments: object, request_id: int = 1) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


class BridgeSurvivesBadInputTests(unittest.TestCase):
    def assert_survived(self, rc: int, responses: list[dict], stderr: str) -> None:
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(len(responses), 2, responses)
        self.assertEqual(responses[1]["id"], 99)
        self.assertIn("tools", responses[1]["result"])

    def test_a_null_timeout_is_a_tool_error_not_a_crash(self) -> None:
        rc, responses, stderr = run_bridge(
            call("shell", {"command": "ls", "timeout_seconds": None}),
            json.dumps(TOOLS_LIST),
        )
        self.assert_survived(rc, responses, stderr)
        self.assertEqual(responses[0]["id"], 1)
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("TypeError", responses[0]["result"]["content"][0]["text"])

    def test_params_that_are_not_an_object_get_invalid_params(self) -> None:
        line = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": []}
        )
        rc, responses, stderr = run_bridge(line, json.dumps(TOOLS_LIST))
        self.assert_survived(rc, responses, stderr)
        self.assertEqual(responses[0]["id"], 3)
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_arguments_that_are_not_an_object_get_invalid_params(self) -> None:
        rc, responses, stderr = run_bridge(
            call("shell", [], request_id=4), json.dumps(TOOLS_LIST)
        )
        self.assert_survived(rc, responses, stderr)
        self.assertEqual(responses[0]["id"], 4)
        self.assertEqual(responses[0]["error"]["code"], -32602)

    def test_a_batch_array_is_refused_as_an_invalid_request(self) -> None:
        rc, responses, stderr = run_bridge(
            json.dumps([TOOLS_LIST]), json.dumps(TOOLS_LIST)
        )
        self.assert_survived(rc, responses, stderr)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_a_line_that_is_not_json_gets_a_parse_error(self) -> None:
        rc, responses, stderr = run_bridge("{not json", json.dumps(TOOLS_LIST))
        self.assert_survived(rc, responses, stderr)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[0]["error"]["code"], -32700)


class HandleNeverRaisesTests(unittest.TestCase):
    def test_an_unexpected_exception_becomes_an_internal_error_response(self) -> None:
        # KeyError is not in the tool-error tuple on purpose: the case here is
        # "a bug nobody anticipated", and the answer is still a response.
        with mock.patch.object(mcp, "call_tool", side_effect=KeyError("boom")):
            response = mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "shell", "arguments": {"command": "ls"}},
                }
            )
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], -32603)
        self.assertIn("KeyError", response["error"]["message"])

    def test_initialize_with_list_params_still_answers(self) -> None:
        response = mcp.handle(
            {"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": []}
        )
        self.assertEqual(response["id"], 8)
        self.assertEqual(
            response["result"]["protocolVersion"], mcp.FALLBACK_PROTOCOL_VERSION
        )


if __name__ == "__main__":
    unittest.main()
