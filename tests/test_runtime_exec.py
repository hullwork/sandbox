"""Process-control invariants of the Runtime MCP server (runtime/runtime_server.py).

The exec functions are called in-process against the real ``/bin/bash``:

* a timed-out command takes its whole process group with it, including
  background children that outlive the shell (``sleep 30 &``);
* captured output stops at ``MAX_OUTPUT_BYTES`` and says so;
* ``pipefail`` is on for every command;
* a PTY shell session that hits its wall-clock budget is killed by
  ``ShellSession.expire`` and by ``SessionManager.reap_idle``.

PTY sessions use ``pty.openpty`` and do not need a controlling terminal, so
nothing here depends on gVisor or on the test runner having a TTY; the whole
module is skipped only when ``/bin/bash`` is missing.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
WORKSPACE_DIR = tempfile.TemporaryDirectory(prefix="w4-runtime-exec-")


def load_runtime_server():
    os.environ.setdefault("SANDBOX_CAPABILITY_KEY", "test-capability-key")
    os.environ.setdefault("SANDBOX_ID", "sb-0123456789ab")
    os.environ.setdefault("WORKSPACE_ID", "ws-123456789abc")
    os.environ["SANDBOX_WORKSPACE"] = WORKSPACE_DIR.name
    os.environ["FILE_SERVICE_WORKSPACE"] = WORKSPACE_DIR.name
    sys.path.insert(0, str(ROOT / "runtime"))
    path = ROOT / "runtime/runtime_server.py"
    spec = importlib.util.spec_from_file_location("runtime_server_exec_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_server = load_runtime_server()
shell_sessions = sys.modules["shell_sessions"]


def process_alive(pid: int) -> bool:
    """True while the pid exists and is not a zombie waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return fields[0] != "Z"


def wait_until_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


@unittest.skipUnless(os.path.exists(BASH), "bash is required for exec tests")
class ExecTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.leaked: list[int] = []

    def tearDown(self) -> None:
        for pid in self.leaked:
            kill_quietly(pid)

    def test_background_child_holding_the_pipe_is_killed_at_timeout(self) -> None:
        started = time.monotonic()
        result = runtime_server.execute_shell({
            "command": "sleep 30 & echo $!; exit 0",
            "timeout_seconds": 1,
        })
        elapsed = time.monotonic() - started
        pid = int(result["stdout"].strip().splitlines()[0])
        self.leaked.append(pid)

        self.assertTrue(result["timed_out"], result)
        self.assertLess(elapsed, 6.0, "timeout must not wait for the background child")
        self.assertTrue(
            wait_until_dead(pid),
            f"background child {pid} survived the timeout",
        )

    def test_foreground_command_is_killed_at_timeout(self) -> None:
        started = time.monotonic()
        result = runtime_server.execute_shell({
            "command": "echo $$; sleep 30; echo unreachable",
            "timeout_seconds": 1,
        })
        elapsed = time.monotonic() - started
        pid = int(result["stdout"].strip().splitlines()[0])
        self.leaked.append(pid)
        self.assertTrue(result["timed_out"])
        self.assertNotIn("unreachable", result["stdout"])
        self.assertLess(elapsed, 6.0)
        self.assertTrue(wait_until_dead(pid))
        self.assertIsNotNone(result["exit_code"])

    def test_closed_pipes_do_not_extend_the_timeout(self) -> None:
        started = time.monotonic()
        result = runtime_server.execute_shell({
            "command": "exec 1>&- 2>&-; sleep 30",
            "timeout_seconds": 1,
        })
        elapsed = time.monotonic() - started
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 6.0)

    def test_fast_command_is_not_reported_as_timed_out(self) -> None:
        result = runtime_server.execute_shell({
            "command": "echo hello; echo oops >&2; exit 3",
            "timeout_seconds": 5,
        })
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["stderr"], "oops\n")
        self.assertFalse(result["output_truncated"])
        self.assertEqual(result["sandbox_id"], os.environ["SANDBOX_ID"])


@unittest.skipUnless(os.path.exists(BASH), "bash is required for exec tests")
class ExecOutputBoundsTests(unittest.TestCase):
    def test_stdout_is_capped_at_max_output_bytes(self) -> None:
        limit = runtime_server.MAX_OUTPUT_BYTES
        result = runtime_server.execute_shell({
            "command": f"head -c {limit * 3} /dev/zero | tr '\\0' a",
            "timeout_seconds": 10,
        })
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["stdout"].encode("utf-8")), limit)
        self.assertTrue(result["output_truncated"])

    def test_stderr_is_capped_and_flagged_too(self) -> None:
        limit = runtime_server.MAX_OUTPUT_BYTES
        result = runtime_server.execute_shell({
            "command": f"head -c {limit + 1} /dev/zero | tr '\\0' b >&2",
            "timeout_seconds": 10,
        })
        self.assertEqual(len(result["stderr"].encode("utf-8")), limit)
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["stdout"], "")

    def test_output_at_exactly_the_limit_is_not_truncated(self) -> None:
        limit = runtime_server.MAX_OUTPUT_BYTES
        result = runtime_server.execute_shell({
            "command": f"head -c {limit} /dev/zero | tr '\\0' c",
            "timeout_seconds": 10,
        })
        self.assertEqual(len(result["stdout"].encode("utf-8")), limit)
        self.assertFalse(result["output_truncated"])

    def test_streaming_delivers_chunks_but_still_caps_the_capture(self) -> None:
        limit = runtime_server.MAX_OUTPUT_BYTES
        chunks: list[tuple[str, str]] = []
        result = runtime_server.execute_shell_stream(
            {"command": f"head -c {limit * 2} /dev/zero | tr '\\0' d", "timeout_seconds": 10},
            lambda channel, text: chunks.append((channel, text)),
        )
        self.assertTrue(result["output_truncated"])
        self.assertEqual(len(result["stdout"].encode("utf-8")), limit)
        streamed = sum(len(text) for channel, text in chunks if channel == "stdout")
        self.assertEqual(streamed, limit * 2)

    def test_pipefail_propagates_the_producer_failure(self) -> None:
        result = runtime_server.execute_shell({
            "command": "false | cat",
            "timeout_seconds": 5,
        })
        self.assertEqual(result["exit_code"], 1)

    def test_argument_validation(self) -> None:
        for arguments in (
            {"command": ""},
            {"command": "true", "timeout_seconds": 0},
            {"command": "true", "timeout_seconds": runtime_server.MAX_EXEC_TIMEOUT_SECONDS + 1},
            {"command": "true", "timeout_seconds": True},
            {"command": "x" * (runtime_server.MAX_COMMAND_BYTES + 1)},
            {"command": "true", "action": "session_exec"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    runtime_server.shell_process_spec(arguments)


@unittest.skipUnless(os.path.exists(BASH), "bash is required for PTY session tests")
class ShellSessionExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = pathlib.Path(WORKSPACE_DIR.name)
        self.env = runtime_server.shell_environment()
        self.sessions: list = []
        self.leaked: list[int] = []

    def tearDown(self) -> None:
        for session in self.sessions:
            session.close()
        for pid in self.leaked:
            kill_quietly(pid)

    def new_session(self, session_id: str = "s-expire"):
        session = shell_sessions.ShellSession(
            session_id, self.workspace, self.env, max_output_chars=64_000
        )
        self.sessions.append(session)
        return session

    def start_runaway(self, session) -> int:
        """Start a foreground child that outlives the wait budget; return its pid.

        Interactive bash puts every job in its own process group, so the
        child is not in the shell's group: it must die with the shell through
        the controlling terminal (SIGHUP to the foreground group) when the
        session is expired.
        """
        result = session.execute(
            "bash -c 'echo $$; exec sleep 30'",
            timeout=2,
            on_chunk=lambda channel, text: None,
            is_cancelled=None,
            async_return=True,
        )
        self.assertTrue(result["running"], result)
        deadline = time.monotonic() + 3
        output = result["stdout"]
        while not re.search(r"\d+", output) and time.monotonic() < deadline:
            time.sleep(0.05)
            output = session.wait(0.2, lambda c, t: None, None)["stdout"]
        match = re.search(r"\d+", output)
        self.assertIsNotNone(match, f"child pid not echoed: {output!r}")
        child = int(match.group(0))
        self.leaked.append(child)
        return child

    def test_expire_marks_the_timeout_and_kills_the_process_group(self) -> None:
        session = self.new_session()
        bash_pid = session.process.pid
        child = self.start_runaway(session)
        self.assertTrue(session.alive)
        self.assertTrue(session.running)

        session.expire()

        self.assertTrue(session.wall_timed_out)
        self.assertFalse(session.running)
        self.assertTrue(wait_until_dead(bash_pid), "session shell survived expire()")
        self.assertTrue(wait_until_dead(child), "foreground child survived expire()")
        self.assertFalse(session.alive)

    def test_reap_idle_expires_a_command_past_the_wall_budget(self) -> None:
        manager = shell_sessions.SessionManager(
            self.workspace,
            self.env,
            max_sessions=2,
            idle_ttl_seconds=3600,
            max_wall_seconds=1,
            cleanup_interval_seconds=0,
        )
        session, restarted = manager.get_or_create("s-wall")
        self.sessions.append(session)
        self.assertFalse(restarted)
        self.assertIs(manager.get("s-wall"), session)
        child = self.start_runaway(session)
        started = session.command_started_at
        self.assertIsNotNone(started)

        # Inside the budget: nothing happens.
        self.assertEqual(manager.reap_idle(now=started + 0.5), 0)
        self.assertTrue(session.running)
        self.assertTrue(manager.activity_snapshot(now=started + 0.5)["busy"])

        # Past the budget: the session is expired and its slot freed.
        self.assertEqual(manager.reap_idle(now=started + 2), 1)
        self.assertTrue(session.wall_timed_out)
        self.assertIsNone(manager.get("s-wall"))
        self.assertTrue(wait_until_dead(session.process.pid))
        self.assertTrue(wait_until_dead(child))
        self.assertEqual(manager.activity_snapshot()["active_sessions"], 0)
        manager.close()

    def test_close_is_idempotent(self) -> None:
        session = self.new_session("s-close")
        session.close()
        session.close()
        self.assertFalse(session.alive)


if __name__ == "__main__":
    unittest.main()
