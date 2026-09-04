"""Behaviour of the persistent PTY shell sessions (runtime/shell_sessions.py).

``test_runtime_exec.py`` covers the one-shot exec path plus the wall-clock
reaper. What a *session* adds on top has no coverage there, and that is what
this module holds: continuity of cwd and exported variables across calls, the
async/input/wait/kill actions, which transport disconnect is allowed to kill a
running command and which is not, the on-disk command file, the ``close()``
ordering that keeps a PTY fd from being handed to the next session while the
old reader thread still holds it, and the rule that a manager must never reap
a process while holding the lock its activity probe needs.

Everything runs against the real ``/bin/bash`` on a real ``pty.openpty`` pair,
so nothing here needs gVisor, a controlling terminal, or extra privileges; the
module is skipped only when ``/bin/bash`` is missing.

Two lines of shell_sessions.py are deliberately left untested, because under
this invocation no test can tell whether they are there. Both were measured,
not assumed:

* ``os.killpg`` in ``close()``. Interactive bash runs job control, so each job
  gets its own process group and the session's group holds nothing but bash
  itself (measured: shell pgid 558546, child pgid 558548). Killing bash alone
  already takes the child down through the controlling terminal. Replacing
  ``os.killpg`` with ``os.kill`` therefore leaves this module and
  test_runtime_exec.py both fully green. The line still matters where job
  control is off and children stay in the shell's group; it just cannot be
  observed from out here.
* ``--noprofile`` in the bash argv. It only suppresses profile files, which a
  non-login shell never reads. Measured with a HOME holding .profile,
  .bashrc and .bash_profile: ``--norc -i`` already reads nothing, while
  ``--noprofile -i`` still reads .bashrc. The flag only becomes observable
  once ``-l`` is added, which this code does not do.

A test for either would have to assert something that is true whatever the
product does, so neither is written.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import shell_sessions  # noqa: E402
from shell_sessions import (  # noqa: E402
    MAX_COMMAND_BYTES,
    SessionCancelled,
    SessionCapacityError,
    SessionManager,
)

BASH = shutil.which("bash") or "/bin/bash"
# A deadlock barrier, not a budget for how fast anything should be: every
# assertion below falls on a condition, this only keeps a hang from pinning the
# whole suite.
DEADLOCK_TIMEOUT = 30.0


def session_environment(workspace: pathlib.Path) -> dict[str, str]:
    """The shape runtime_server.shell_environment() hands the manager."""
    return {
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PWD": str(workspace),
        "PS1": "",
        "PS2": "",
        "PROMPT_COMMAND": "",
        "TERM": "dumb",
    }


def process_alive(pid: int) -> bool:
    """Alive means scheduled: a zombie has been killed and is merely unreaped."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            state = handle.read().rsplit(b")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != b"Z"


def collector() -> tuple[list[tuple[str, str]], object]:
    chunks: list[tuple[str, str]] = []
    return chunks, lambda channel, text: chunks.append((channel, text))


@unittest.skipUnless(os.path.exists(BASH), "bash is required for PTY session tests")
class ShellSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-shell-session-")
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = pathlib.Path(self.tempdir.name)
        self.manager = SessionManager(
            self.workspace,
            session_environment(self.workspace),
            max_sessions=2,
            idle_ttl_seconds=30,
            cleanup_interval_seconds=0,
            max_output_chars=64_000,
            max_timeout_seconds=3,
            max_wait_timeout_seconds=5,
        )
        self.addCleanup(self.manager.close)

    def test_exec_reuses_cwd_and_environment_without_leaking_marker(self) -> None:
        chunks, on_chunk = collector()
        first = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "main",
                "command": "mkdir -p nested && cd nested && export PTY_VALUE=kept",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        self.assertFalse(first["running"])

        chunks.clear()
        second = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "main",
                "command": "printf '%s:%s' \"$PWD\" \"$PTY_VALUE\"",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        output = "".join(text for _, text in chunks)
        self.assertIn("/nested:kept", output)
        # The completion marker this repo emits is SANDBOX_DONE_<token>; the
        # reader has to strip it out of the stream before the caller sees it.
        self.assertNotIn("SANDBOX_DONE", output)
        self.assertEqual(second["exit_code"], 0)

    def test_pipefail_is_on_in_the_session_shell(self) -> None:
        """The exec path sets pipefail per command; the session sets it once.

        ``test_runtime_exec.py`` guards the ``set -o pipefail`` prepended by
        execute_shell_stream. The line guarded here is a different one: the
        PROMPT_COMMAND preamble written into the interactive shell at startup.
        Without it ``head`` reports 0 and a failed producer is invisible.
        """
        chunks, on_chunk = collector()
        result = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "pipefail",
                "command": "(exit 9) | head -1",
                "timeout_seconds": 3,
            },
            on_chunk,
        )
        self.assertEqual(result["exit_code"], 9)

    def test_async_exec_accepts_input_and_finishes_on_later_call(self) -> None:
        chunks, on_chunk = collector()
        started = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "interactive",
                "command": "read value; printf 'got:%s\\n' \"$value\"",
                "timeout_seconds": 2,
                "async": True,
            },
            on_chunk,
        )
        self.assertTrue(started["running"])

        chunks.clear()
        finished = self.manager.handle(
            {
                "action": "session_input",
                "session_id": "interactive",
                "input": "hello",
                "append_newline": True,
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        self.assertFalse(finished["running"])
        self.assertEqual(finished["exit_code"], 0)
        self.assertIn("got:hello", "".join(text for _, text in chunks))

    def test_wait_deadline_does_not_kill_the_running_command(self) -> None:
        chunks, on_chunk = collector()
        session, _ = self.manager.get_or_create("long")
        first = session.execute(
            "sleep 0.35; printf done",
            # The behaviour under test is that the deadline arrives first and
            # nothing is killed. Relax this 0.05 and the test proves nothing.
            0.05,
            on_chunk,
            None,
            async_return=False,
        )
        self.assertTrue(first["running"])
        self.assertTrue(first["wait_timed_out"])
        # ``timed_out`` is reserved for the wall-clock reaper; a wait deadline
        # is not a command timeout.
        self.assertFalse(first["timed_out"])
        self.assertTrue(session.alive)

        chunks.clear()
        finished = session.wait(DEADLOCK_TIMEOUT, on_chunk, None)
        self.assertFalse(finished["running"])
        self.assertIn("done", "".join(text for _, text in chunks))

    def test_cancel_kills_the_session_and_the_next_exec_restarts_it(self) -> None:
        chunks, on_chunk = collector()
        started = time.monotonic()
        with self.assertRaises(SessionCancelled):
            self.manager.handle(
                {
                    "action": "session_exec",
                    "session_id": "cancel-me",
                    "command": "sleep 30",
                    "timeout_seconds": 3,
                },
                on_chunk,
                lambda: time.monotonic() - started > 0.1,
            )
        self.assertIsNone(self.manager.get("cancel-me"))

        result = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "cancel-me",
                "command": "printf restarted",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        # Restarted, not merely created: the dead session was still in the
        # table and the caller is told its shell is a new one.
        self.assertTrue(result["session_restarted"])
        self.assertFalse(result["running"])

    def test_output_delivery_failure_kills_the_active_session(self) -> None:
        def disconnected(_channel: str, _text: str) -> None:
            raise BrokenPipeError("client disconnected")

        with self.assertRaises(BrokenPipeError):
            self.manager.handle(
                {
                    "action": "session_exec",
                    "session_id": "broken-stream",
                    "command": "printf ready; sleep 30",
                    "timeout_seconds": 2,
                },
                disconnected,
            )
        self.assertIsNone(self.manager.get("broken-stream"))

    def test_input_is_rejected_when_no_command_is_running(self) -> None:
        chunks, on_chunk = collector()
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "safe-input",
                "command": "true",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        # Without the guard this line is typed straight at an idle prompt and
        # runs as a command nobody asked for.
        with self.assertRaisesRegex(ValueError, "no running command"):
            self.manager.handle(
                {
                    "action": "session_input",
                    "session_id": "safe-input",
                    "input": "echo bypass",
                    "timeout_seconds": 2,
                },
                on_chunk,
            )

    def test_a_long_command_bypasses_the_pty_line_limit(self) -> None:
        """User code travels through a 0600 file, not the terminal line.

        A canonical PTY line is commonly capped near 1 KiB, so an otherwise
        valid command can block or be truncated even when every non-blocking
        write returns success. Only a short source line reaches the terminal.
        """
        marker = "z" * 2_000
        chunks, on_chunk = collector()
        result = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "long-write",
                "command": f"printf '%s' {marker}",
                "timeout_seconds": 3,
            },
            on_chunk,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["running"])
        self.assertIn(marker, "".join(text for _, text in chunks))
        self.assertEqual(list(self.workspace.glob(".sandbox-command-*.sh")), [])

    def test_exec_and_wait_have_distinct_rejected_limits(self) -> None:
        chunks, on_chunk = collector()
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            self.manager.handle(
                {
                    "action": "session_exec",
                    "session_id": "limits",
                    "command": "true",
                    "timeout_seconds": 4,
                },
                on_chunk,
            )
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "limits",
                "command": "sleep 0.1; printf done",
                "timeout_seconds": 3,
                "async": True,
            },
            on_chunk,
        )
        # 4 is over the exec limit and under the wait limit: one number that
        # only passes if the two limits are really separate.
        result = self.manager.handle(
            {
                "action": "session_wait",
                "session_id": "limits",
                "timeout_seconds": 4,
            },
            on_chunk,
        )
        self.assertFalse(result["running"])
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            self.manager.handle(
                {
                    "action": "session_wait",
                    "session_id": "limits",
                    "timeout_seconds": 6,
                },
                on_chunk,
            )

    def test_session_exec_rejects_an_oversized_command(self) -> None:
        chunks, on_chunk = collector()
        with self.assertRaisesRegex(ValueError, "command is too large"):
            self.manager.handle(
                {
                    "action": "session_exec",
                    "session_id": "too-long",
                    "command": "x" * (MAX_COMMAND_BYTES + 1),
                    "timeout_seconds": 2,
                },
                on_chunk,
            )
        # Rejected before anything is spawned: a refused command must not cost
        # a session slot.
        self.assertIsNone(self.manager.get("too-long"))

    def test_the_command_file_is_removed_when_user_code_exits_the_shell(self) -> None:
        """PROMPT_COMMAND never runs when the command replaces the shell.

        The reader's teardown is the only path that can clean up then, and
        without it every ``exit`` leaves a 0600 script in the workspace.
        """
        chunks, on_chunk = collector()
        result = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "exit-shell",
                "command": "exit 7",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(list(self.workspace.glob(".sandbox-command-*.sh")), [])

    def test_wall_clock_expiry_is_reported_to_the_next_wait(self) -> None:
        """The reaping itself is covered by test_runtime_exec.py.

        What is only here: the *reason* survives into the result the caller
        reads back, instead of an always-False ``timed_out``.
        """
        chunks, on_chunk = collector()
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "runaway",
                "command": "sleep 30",
                "timeout_seconds": 2,
                "async": True,
            },
            on_chunk,
        )
        session = self.manager.get("runaway")
        self.assertTrue(session.running)
        # The idle TTL never reaches a running command: ``running`` only clears
        # on the completion marker.
        self.assertEqual(self.manager.reap_idle(), 0)

        self.manager.max_wall_seconds = 0.05
        self.assertEqual(self.manager.reap_idle(), 1)
        self.assertIsNone(self.manager.get("runaway"))
        self.assertTrue(session.wait(1, on_chunk, None)["timed_out"])

    def test_a_wait_disconnect_leaves_the_running_command_alive(self) -> None:
        """exec owns its command, wait does not.

        A wait is re-addressed by session_id, so a cut stream only means nobody
        collects the output this time. Killing there lets any network jitter
        destroy a ten-minute build along with its cwd and exported variables.
        """
        chunks, on_chunk = collector()
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "keep-running",
                "command": "sleep 0.4; printf finished",
                "timeout_seconds": 2,
                "async": True,
            },
            on_chunk,
        )
        with self.assertRaises(SessionCancelled):
            self.manager.handle(
                {
                    "action": "session_wait",
                    "session_id": "keep-running",
                    "timeout_seconds": 2,
                },
                on_chunk,
                lambda: True,
            )
        session = self.manager.get("keep-running")
        self.assertIsNotNone(session)
        self.assertTrue(session.alive)

        chunks.clear()
        finished = self.manager.handle(
            {
                "action": "session_wait",
                "session_id": "keep-running",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        self.assertFalse(finished["running"])
        self.assertEqual(finished["exit_code"], 0)
        self.assertIn("finished", "".join(text for _, text in chunks))

    def test_kill_restarts_the_same_session_id(self) -> None:
        chunks, on_chunk = collector()
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "kill-me",
                "command": "sleep 30",
                "timeout_seconds": 2,
                "async": True,
            },
            on_chunk,
        )
        result = self.manager.handle(
            {
                "action": "session_kill",
                "session_id": "kill-me",
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        self.assertTrue(result["session_restarted"])
        self.assertFalse(result["running"])
        # The id keeps working: kill is a restart, not a delete.
        replacement = self.manager.get("kill-me")
        self.assertIsNotNone(replacement)
        self.assertTrue(replacement.alive)

    def test_close_reaps_the_killed_process_even_when_the_first_wait_expires(
        self,
    ) -> None:
        """SIGKILL was already sent, so somebody has to wait it out.

        Swallowing the TimeoutExpired leaves a zombie nobody will ever reap.
        The first wait is forced to time out here and the assertion is that a
        second one still happens.
        """
        session, _ = self.manager.get_or_create("reap-me")
        process = session.process
        real_wait = process.wait
        calls: list[float | None] = []
        reaped = threading.Event()

        def flaky_wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="bash", timeout=timeout or 0)
            try:
                return real_wait(timeout=timeout)
            finally:
                reaped.set()

        with mock.patch.object(process, "wait", flaky_wait):
            session.close()
            self.assertTrue(
                reaped.wait(timeout=DEADLOCK_TIMEOUT),
                "nobody reaped after the first wait timed out - "
                "SIGKILL was already sent, so that is a zombie",
            )
        self.assertIsNotNone(process.returncode)

    @unittest.skipUnless(
        os.path.isdir("/proc"),
        "background-session cleanup is a Linux Runtime contract",
    )
    def test_close_kills_a_background_job_the_shell_left_behind(self) -> None:
        """``cmd &`` survives both killpg calls: interactive bash gives it a
        process group of its own, and it is not the foreground group either.
        Before this the only thing that ended it was the Pod's TTL.
        """
        chunks, on_chunk = collector()
        result = self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "bg",
                # Far longer than the wait below: a job that merely ran out
                # would pass this test for the wrong reason.
                "command": "sleep 600 >/dev/null 2>&1 & printf 'pid=%s\\n' $!",
                "timeout_seconds": 3,
            },
            on_chunk,
        )
        self.assertFalse(result["running"])
        match = re.search(r"pid=(\d+)", "".join(text for _, text in chunks))
        self.assertIsNotNone(match, chunks)
        pid = int(match.group(1))
        self.addCleanup(lambda: process_alive(pid) and os.kill(pid, 9))
        self.assertTrue(process_alive(pid), "the background job should be up")

        self.assertTrue(self.manager.cancel("bg"))
        deadline = time.monotonic() + 10.0
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), f"background job {pid} outlived close()")

    def test_close_keeps_the_pty_fd_while_the_reader_still_holds_it(self) -> None:
        """Closing master_fd under a live reader hands it to the next openpty.

        The old reader then reads another session's terminal. Leaking one fd is
        the cheaper failure, and there is an upper bound on sessions anyway.
        """
        session, _ = self.manager.get_or_create("fd-order")
        stuck = threading.Event()
        squatter = threading.Thread(
            target=stuck.wait, name="reader-stand-in", daemon=True
        )
        squatter.start()
        real_reader = session._reader
        master_fd = session.master_fd

        def restore() -> None:
            stuck.set()
            squatter.join(timeout=5)
            session._reader = real_reader
            real_reader.join(timeout=5)
            # Do not close an fd number that close() may already have released:
            # by now it can belong to somebody else.
            try:
                os.fstat(master_fd)
            except OSError:
                return
            os.close(master_fd)

        self.addCleanup(restore)
        session._reader = squatter
        with mock.patch.object(shell_sessions, "READER_JOIN_SECONDS", 0.2):
            session.close()
        os.fstat(master_fd)  # fstat only succeeds while the fd is still open.

    def test_close_releases_the_pty_fd_once_the_reader_is_gone(self) -> None:
        session, _ = self.manager.get_or_create("fd-release")
        master_fd = session.master_fd
        session.close()
        self.assertFalse(session._reader.is_alive())
        with self.assertRaises(OSError):
            os.fstat(master_fd)


@unittest.skipUnless(os.path.exists(BASH), "bash is required for PTY session tests")
class SessionManagerEvictionTests(unittest.TestCase):
    """Eviction must not reap a process while holding the manager lock.

    ``close()`` budgets KILL_REAP_SECONDS plus READER_JOIN_SECONDS, and
    ``activity_snapshot`` takes the same lock, while the Control Plane's
    activity probe waits two seconds and treats a timeout as "deletable". One
    eviction would then delete a Runtime that is still doing work, taking every
    session in it. Nothing is faked here: only the child killed by SIGKILL is
    made slow to be collected, which is the condition KILL_REAP_SECONDS is
    written for.
    """

    SLOW_REAP = 2.0
    # The bar sits inside the probe's budget with margin: microseconds when the
    # lock is released before reaping, at least SLOW_REAP when it is not.
    PROBE_BUDGET = 1.0

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="w4-shell-evict-")
        self.addCleanup(self.tempdir.cleanup)
        workspace = pathlib.Path(self.tempdir.name)
        self.manager = SessionManager(
            workspace,
            session_environment(workspace),
            max_sessions=1,
            idle_ttl_seconds=30,
            cleanup_interval_seconds=0,
            max_output_chars=64_000,
        )
        self.addCleanup(self.manager.close)

    def test_activity_snapshot_is_not_blocked_by_an_eviction(self) -> None:
        victim, _ = self.manager.get_or_create("victim")
        real_wait = victim.process.wait
        entered = threading.Event()

        def slow_wait(timeout=None):
            # This is the process.wait(timeout=KILL_REAP_SECONDS) inside close().
            entered.set()
            time.sleep(self.SLOW_REAP)
            return real_wait(timeout=timeout)

        victim.process.wait = slow_wait
        evicted = threading.Event()

        def evict() -> None:
            # One slot only, so this evicts the victim.
            self.manager.get_or_create("newcomer")
            evicted.set()

        worker = threading.Thread(target=evict, name="evictor", daemon=True)
        worker.start()
        self.addCleanup(worker.join, DEADLOCK_TIMEOUT)
        self.assertTrue(
            entered.wait(timeout=DEADLOCK_TIMEOUT),
            "the eviction thread never entered close()",
        )

        began = time.monotonic()
        snapshot = self.manager.activity_snapshot()
        elapsed = time.monotonic() - began
        self.assertLess(
            elapsed,
            self.PROBE_BUDGET,
            f"/activity was blocked {elapsed:.2f}s by the lock the eviction "
            "holds; the probe waits two seconds and deletes a Runtime that is "
            "still working when it times out",
        )
        # idle_seconds is a relative count, not a timestamp; blocking on the
        # lock would push it negative.
        self.assertGreaterEqual(snapshot["idle_seconds"], 0.0)
        self.assertTrue(evicted.wait(timeout=DEADLOCK_TIMEOUT))

    def test_a_full_manager_reports_capacity_before_creating_anything(self) -> None:
        """Every slot busy: refuse, and leave no unreachable session behind.

        A stale session with the same id is popped from the table inside the
        lock. If the refusing path skips its close(), that bash and its PTY
        live to the end of the Pod with nobody able to address them.
        """
        chunks, on_chunk = collector()
        self.manager.handle(
            {
                "action": "session_exec",
                "session_id": "busy",
                "command": "sleep 30",
                "async": True,
                "timeout_seconds": 2,
            },
            on_chunk,
        )
        with self.assertRaisesRegex(SessionCapacityError, "shell sessions are busy"):
            self.manager.get_or_create("newcomer")
        self.assertIsNotNone(self.manager.get("busy"))
        self.assertEqual(self.manager.activity_snapshot()["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
