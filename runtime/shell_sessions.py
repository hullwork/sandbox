"""Bounded persistent PTY sessions for the Sandbox Runtime.

The MCP transport is intentionally stateless.  Session continuity is keyed by
the explicit ``session_id`` tool argument and lives only for the lifetime of a
single gVisor Runtime Pod.
"""
from __future__ import annotations

import codecs
import collections
import os
import pty
import re
import select
import shlex
import signal
import subprocess
import termios
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SESSION_ACTIONS = {
    "session_exec",
    "session_wait",
    "session_input",
    "session_kill",
}
MAX_INPUT_BYTES = 8_192
#Keep the same upper limit as non-session paths (runtime_server.MAX_COMMAND_BYTES). missing it
#The upper limit of the body (64000) is the upper limit of the command, and PTY cannot write such a long line at a time.
MAX_COMMAND_BYTES = 8_192
#How long it takes to write PTY is considered a failure. If the next line in canonical mode exceeds MAX_CANON, it will no longer be queued.
#Rather than silently discarding the second half of the command, it is better to report an error.
WRITE_TIMEOUT_SECONDS = 10.0
#How long does it take to wait after SIGKILL to calculate "the kernel has not been recycled yet". SIGKILL cannot be captured, and the process cannot do anything during this waiting period.
#For anything, the duration is only determined by scheduling and memory recycling - and Runtime runs in gVisor + 500m CPU,
#The original 1s is not enough at full load. There is no cost in waiting longer, and if you wait not enough, zombie processes will be left behind.
KILL_REAP_SECONDS = 10.0
#The upper limit of reader thread closing. After _closed is set, it can complete one round of select at most (0.1s), 5s is
#margin under full load.
READER_JOIN_SECONDS = 5.0
#How long to wait after the child process exits is considered "the kernel has not made it waitable". EOF/EIO on the PTY side becomes
#For zombies, this wait only covers that window, which is normally microseconds.
EXIT_REAP_SECONDS = 5.0


class SessionCancelled(ConnectionError):
    """The client disconnected while a session action was active."""


class SessionCapacityError(RuntimeError):
    """All PTY session slots are occupied by active commands."""


def _validate_timeout(value: Any, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("timeout_seconds must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"timeout_seconds must be between 1 and {maximum}")
    return value


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "session_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
        )
    return value


class ShellSession:
    """One persistent bash process attached to a bounded PTY output ring."""

    def __init__(
        self,
        session_id: str,
        workspace: Path,
        env: dict[str, str],
        *,
        max_output_chars: int,
    ) -> None:
        self.session_id = session_id
        self.workspace = workspace
        self.env = dict(env)
        self.max_output_chars = max_output_chars
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        #The starting time of the current command, the wall clock is used; None = there is no running command
        self.command_started_at: float | None = None
        self.wall_timed_out = False

        self._condition = threading.Condition(threading.RLock())
        self._operation_lock = threading.Lock()
        self._chunks: collections.deque[tuple[int, str]] = collections.deque()
        self._base_offset = 0
        self._next_offset = 0
        self._buffer_chars = 0
        self._consumer_offset = 0
        self._parse_buffer = b""
        self._marker_prefix: bytes | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._running = False
        self._closed = False
        self._last_exit_code: int | None = None
        self._command_file: Path | None = None

        master_fd, slave_fd = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~(termios.ECHO | termios.ECHONL)
            if hasattr(termios, "ONLCR"):
                attrs[1] &= ~termios.ONLCR
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
            self.process = subprocess.Popen(
                ["/bin/bash", "--noprofile", "--norc", "-i"],
                cwd=self.workspace,
                env=self.env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

        self.master_fd = master_fd
        os.set_blocking(self.master_fd, False)
        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"shell-session-{session_id}",
            daemon=True,
        )
        self._reader.start()
        # PROMPT_COMMAND runs only after the active command has returned.  This
        # avoids queueing marker commands behind user code: an interactive
        # ``read`` would otherwise consume those control lines as user input.
        #
        #🔴 AI-LOCK: The exit_code reported by the session path is **self-reported within the session**, not a trusted value.
        #Don't put an "anti-counterfeit" patch on it - that will just make the next person think it's trustworthy.
        #The completion mark is hit on stdout by this PROMPT_COMMAND, and the token goes to the shell
        #The variable __sandbox_token is passed in; the user command and this hook are in the same bash
        #In the process, you can read the token by saying `echo $__sandbox_token`, and then
        #printf A fake DONE makes the Python side think that the command has ended and the exit code is 0.
        #This is unstoppable: removing the token from the command file name does not raise the threshold (the variables are still readable).
        #Changing the out-of-band fd won't work (same as uid, users can still write in), and the permissions are even more unstoppable.
        #This is an inherent boundary of interactive PTY models, not an oversight in this code.
        #**Callers who need a trusted exit code must go exec**: that path's exit_code comes from
        #process.returncode(runtime_server.shell_result) of subprocess, by
        #Given by the kernel, code within the sandbox cannot affect it.
        #To make the session path credible, you can only change the execution model (the command no longer sources into this interactive
        #shell), at the cost of losing continuity between cwd and exported variables - that was a product decision,
        #Not a bug fix.
        prompt_setup = (
            "set -o pipefail\n"
            "__sandbox_armed=0\n"
            "__sandbox_command_file=\n"
            "__sandbox_prompt() {\n"
            "  __sandbox_status=$?\n"
            '  if [ "${__sandbox_armed:-0}" = 1 ]; then\n'
            '    if [ -n "${__sandbox_command_file:-}" ]; then\n'
            '      rm -f -- "$__sandbox_command_file"\n'
            "      __sandbox_command_file=\n"
            "    fi\n"
            "    printf '\\036SANDBOX_DONE_%s:%s\\n' "
            '"$__sandbox_token" "$__sandbox_status"\n'
            "    __sandbox_armed=0\n"
            "  fi\n"
            "}\n"
            "PROMPT_COMMAND=__sandbox_prompt\n"
        )
        self._write(prompt_setup.encode("utf-8"))

    def _create_command_file(self, command: str, token: str) -> Path:
        """Store user code outside the PTY's canonical input line.

        macOS commonly limits one canonical PTY line to 1024 bytes. Writing an
        otherwise valid 8 KiB command directly to the terminal can therefore
        block or truncate even when every non-blocking write succeeds. The
        interactive shell only receives a short source command; sourcing keeps
        cwd and exported variables in the persistent shell.
        """
        path = self.workspace / f".sandbox-command-{token}.sh"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(command.encode("utf-8"))
                if not command.endswith("\n"):
                    stream.write(b"\n")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _cleanup_command_file_locked(self) -> None:
        path = self._command_file
        self._command_file = None
        if path is not None:
            path.unlink(missing_ok=True)

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.poll() is None

    @property
    def running(self) -> bool:
        with self._condition:
            return self._running

    def _append_text_locked(self, text: str) -> None:
        if not text:
            return
        start = self._next_offset
        self._chunks.append((start, text))
        self._next_offset += len(text)
        self._buffer_chars += len(text)
        while self._buffer_chars > self.max_output_chars and self._chunks:
            over = self._buffer_chars - self.max_output_chars
            first_start, first_text = self._chunks[0]
            if len(first_text) <= over:
                self._chunks.popleft()
                self._buffer_chars -= len(first_text)
                self._base_offset = first_start + len(first_text)
                continue
            self._chunks[0] = (
                first_start + over,
                first_text[over:],
            )
            self._buffer_chars -= over
            self._base_offset = first_start + over

    def _append_bytes_locked(self, payload: bytes, *, final: bool = False) -> None:
        text = self._decoder.decode(payload, final=final)
        self._append_text_locked(text)

    @staticmethod
    def _marker_suffix_length(payload: bytes, marker: bytes) -> int:
        maximum = min(len(payload), len(marker) - 1)
        for length in range(maximum, 0, -1):
            if payload[-length:] == marker[:length]:
                return length
        return 0

    def _consume_pty_bytes_locked(self, payload: bytes) -> None:
        self._parse_buffer += payload
        while self._parse_buffer:
            marker = self._marker_prefix
            if marker is None:
                self._append_bytes_locked(self._parse_buffer)
                self._parse_buffer = b""
                return

            marker_index = self._parse_buffer.find(marker)
            if marker_index < 0:
                keep = self._marker_suffix_length(self._parse_buffer, marker)
                visible = self._parse_buffer[:-keep] if keep else self._parse_buffer
                self._append_bytes_locked(visible)
                self._parse_buffer = self._parse_buffer[-keep:] if keep else b""
                return

            self._append_bytes_locked(self._parse_buffer[:marker_index])
            remainder = self._parse_buffer[marker_index + len(marker):]
            newline = remainder.find(b"\n")
            if newline < 0:
                self._parse_buffer = self._parse_buffer[marker_index:]
                return

            raw_status = remainder[:newline].strip(b"\r ")
            try:
                exit_code = int(raw_status.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                exit_code = -1
            self._last_exit_code = exit_code
            self._running = False
            self._marker_prefix = None
            self._cleanup_command_file_locked()
            self.command_started_at = None
            self.last_activity = time.monotonic()
            self._parse_buffer = remainder[newline + 1:]
            self._condition.notify_all()

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                if self.process.poll() is not None:
                    break
                readable, _, _ = select.select([self.master_fd], [], [], 0.1)
                if not readable:
                    continue
                try:
                    payload = os.read(self.master_fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not payload:
                    break
                with self._condition:
                    self._consume_pty_bytes_locked(payload)
                    self.last_activity = time.monotonic()
                    self._condition.notify_all()
        finally:
            #PTY's EOF/EIO becomes a waitable zombie earlier than the child process, so non-blocking poll()
            #This will return None - there is no path to the marker (user code replaces the shell itself with
            #There is only one source for the exit code of exit), and that None will be left permanently as the final state.
            #It does not wait when _closed is set: at that time, close() is waiting to collect the corpse, and it will block again here.
            #will just drag its _reader.join to timeout. Do it outside the lock and not block while holding the lock.
            exit_status = self.process.poll()
            if exit_status is None and not self._closed:
                try:
                    exit_status = self.process.wait(timeout=EXIT_REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    exit_status = None
            with self._condition:
                if self._parse_buffer:
                    self._append_bytes_locked(self._parse_buffer)
                    self._parse_buffer = b""
                self._append_bytes_locked(b"", final=True)
                self._running = False
                self._marker_prefix = None
                self._cleanup_command_file_locked()
                self.command_started_at = None
                self.last_activity = time.monotonic()
                if self._last_exit_code is None:
                    self._last_exit_code = exit_status
                self._condition.notify_all()

    def _read_from_locked(self, cursor: int) -> tuple[list[str], int, bool]:
        truncated = cursor < self._base_offset
        cursor = max(cursor, self._base_offset)
        visible: list[str] = []
        for start, text in self._chunks:
            end = start + len(text)
            if end <= cursor:
                continue
            part = text[max(0, cursor - start):]
            if part:
                visible.append(part)
            cursor = end
        return visible, cursor, truncated

    def _write(self, payload: bytes) -> None:
        """Write the whole payload to the non-blocking PTY master.

        The fd is non-blocking (see __init__), so a single os.write returns a
        short count whenever the line discipline's buffer fills up. Dropping
        that count silently truncates the command: bash then receives a line
        with no terminating newline, never runs it, and the *next* session_exec
        gets concatenated onto the leftover — two commands executed as one,
        with no error anywhere in the chain.
        """
        if not self.alive:
            raise RuntimeError("shell session is not alive")
        view = memoryview(payload)
        deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
        while view:
            try:
                written = os.write(self.master_fd, view)
            except BlockingIOError:
                written = 0
            except OSError as exc:
                raise RuntimeError(f"write to shell session failed: {exc}") from exc
            if written:
                view = view[written:]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "write to shell session timed out with "
                    f"{len(view)} bytes left"
                )
            select.select([], [self.master_fd], [], min(0.05, remaining))

    def _collect(
        self,
        action: str,
        timeout: float,
        on_chunk: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] | None,
        *,
        async_return: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (min(timeout, 0.2) if async_return else timeout)
        output_parts: list[str] = []
        output_chars = 0
        output_truncated = False

        #exec owns the command it just started: once the caller is disconnected, no one can address it anymore, it can only connect
        #The process group is collected together. wait/input is just the opposite - they rely on session_id to re-find
        #If there is already a command at the address, the interruption of the stream only means that no one will receive the stream return this time, and the wait will continue next time.
        #Read; kill here means that any network jitter can kill a ten-minute build.
        owns_command = action == "exec"

        while True:
            if is_cancelled is not None and is_cancelled():
                if owns_command:
                    self.cancel_active()
                raise SessionCancelled("shell session client disconnected")

            with self._condition:
                chunks, cursor, ring_truncated = self._read_from_locked(
                    self._consumer_offset
                )
                self._consumer_offset = cursor
                running = self._running
                exit_code = self._last_exit_code
                output_truncated = output_truncated or ring_truncated

            for chunk in chunks:
                try:
                    on_chunk("stdout", chunk)
                except BaseException:
                    # If the HTTP/SSE consumer disappears while output is being
                    # delivered, the caller can no longer address this command.
                    # Tear down the whole process group instead of leaving an
                    # orphaned interactive task behind — but only for the exec
                    # that started it, for the same reason as above.
                    if owns_command:
                        self.cancel_active()
                    raise
                remaining = self.max_output_chars - output_chars
                if remaining > 0:
                    output_parts.append(chunk[:remaining])
                    output_chars += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    output_truncated = True

            #Only recognize _running: the process disappears one step earlier than the reader closes (the PTY's EOF/EIO comes first
            #For child processes, wait), using alive will bring the exit code before the reader fills in the exit code.
            #running=True, exit_code=None returns - that exit code can never be retrieved again.
            #Each write point of _running covers the scenario where the process disappears (marker / reader's
            #finally / close), so it will definitely come; the deadline will take care of things that are really stuck.
            if not running:
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                break
            with self._condition:
                self._condition.wait(timeout=min(0.05, remaining_time))

        wait_timed_out = running and not async_return
        self.last_activity = time.monotonic()
        return {
            "action": action,
            "session_id": self.session_id,
            "exit_code": None if running else exit_code,
            "stdout": "".join(output_parts),
            "stderr": "",
            "running": running,
            #The real "timed_out" is when the wall clock is recycled at that time; when the deadline of wait is reached, it is just this
            #If it is not finished reading at once and the command is still running, it is wait_timed_out.
            "timed_out": self.wall_timed_out,
            "wait_timed_out": wait_timed_out,
            "output_truncated": output_truncated,
        }

    def execute(
        self,
        command: str,
        timeout: float,
        on_chunk: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] | None,
        *,
        async_return: bool,
    ) -> dict[str, Any]:
        with self._operation_lock:
            token = uuid.uuid4().hex
            command_file = self._create_command_file(command, token)
            with self._condition:
                if self._running:
                    command_file.unlink(missing_ok=True)
                    raise ValueError(
                        "session already has a running command; use wait, input, or kill"
                    )
                self._consumer_offset = self._next_offset
                marker_text = f"\x1eSANDBOX_DONE_{token}:"
                self._marker_prefix = marker_text.encode("ascii")
                self._command_file = command_file
                self._last_exit_code = None
                self._running = True
                self.wall_timed_out = False
                self.command_started_at = time.monotonic()
                self.last_activity = self.command_started_at

            quoted_path = shlex.quote(str(command_file))
            script = (
                f"__sandbox_token={token}; "
                f"__sandbox_command_file={quoted_path}; "
                '__sandbox_armed=1; set +e; . "$__sandbox_command_file"\n'
            )
            try:
                self._write(script.encode("utf-8"))
            except BaseException:
                with self._condition:
                    self._running = False
                    self._marker_prefix = None
                    self._cleanup_command_file_locked()
                    self.command_started_at = None
                    self._condition.notify_all()
                raise
            return self._collect(
                "exec",
                timeout,
                on_chunk,
                is_cancelled,
                async_return=async_return,
            )

    def wait(
        self,
        timeout: float,
        on_chunk: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        with self._operation_lock:
            return self._collect("wait", timeout, on_chunk, is_cancelled)

    def send_input(
        self,
        value: str,
        append_newline: bool,
        timeout: float,
        on_chunk: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        with self._operation_lock:
            with self._condition:
                if not self._running:
                    raise ValueError("session has no running command")
            payload = value + ("\n" if append_newline else "")
            self._write(payload.encode("utf-8"))
            return self._collect("input", timeout, on_chunk, is_cancelled)

    def cancel_active(self) -> None:
        self.close()

    def expire(self) -> None:
        """Kill a command that outlived its wall-clock budget, and say so."""
        with self._condition:
            self.wall_timed_out = True
        self.close()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._running = False
            self._marker_prefix = None
            self._cleanup_command_file_locked()
            self.command_started_at = None
            self._condition.notify_all()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=KILL_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            #SIGKILL has been sent. If you don't wait, the zombie process will be left behind. Not here
            #After swallowing the timeout, the caller's thread cannot continue to be occupied - it is handed over to the background thread to wait until the kernel
            #Until it is actually recycled.
            threading.Thread(
                target=self.process.wait,
                name=f"shell-session-reap-{self.session_id}",
                daemon=True,
            ).start()
        #Before closing master_fd, wait for the reader thread to close: it is still selecting / reading this fd,
        #After closing, the fd number will be reused by the next pty.openpty(), and the old reader will read other
        #The session's PTY is up. If you can't wait, you'd rather miss a fd (there is an upper limit on the number of sessions) than wait.
        #Someone else returned it when they used this number.
        self._reader.join(timeout=READER_JOIN_SECONDS)
        if self._reader.is_alive():
            return
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class SessionManager:
    """Thread-safe, bounded owner of Runtime-local PTY sessions."""

    def __init__(
        self,
        workspace: Path,
        env: dict[str, str],
        *,
        max_sessions: int = 16,
        idle_ttl_seconds: int = 1_800,
        max_wall_seconds: float = 3_600,
        cleanup_interval_seconds: int = 60,
        max_output_chars: int = 64_000,
        max_timeout_seconds: int = 30,
        max_wait_timeout_seconds: int = 120,
    ) -> None:
        self.workspace = workspace
        self.env = dict(env)
        self.max_sessions = max_sessions
        self.idle_ttl_seconds = idle_ttl_seconds
        self.max_wall_seconds = max_wall_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.max_output_chars = max_output_chars
        self.max_timeout_seconds = max_timeout_seconds
        self.max_wait_timeout_seconds = max_wait_timeout_seconds
        self._sessions: dict[str, ShellSession] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        if cleanup_interval_seconds > 0:
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="shell-session-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._closed.wait(self.cleanup_interval_seconds):
            self.reap_idle()

    def activity_snapshot(self, now: float | None = None) -> dict:
        """Interface model: An active snapshot for Control Plane to decide whether to recycle this runtime.

        Responsibility: truthfully report whether there is any activity inside this Runtime; not responsible for recycling decisions
             (That's a matter of Control Plane's reaper, only the basis is provided here).

        Fields:
          active_sessions: Number of PTY sessions currently held
          running_commands: Number of sessions in which commands are being executed
          idle_seconds: The number of seconds since the last session activity; None when there is no session
          busy: whether recycling should be blocked

        AI-LOCK: ``idle_seconds`` is a **relative number of seconds** not a timestamp. session side
             ``last_activity`` is taken from ``time.monotonic()`` and is only meaningful within this process.
             Cross-process comparisons give ridiculous results. The clock source must be changed before changing to absolute timestamp.

        Constraint: The criterion of busy has the same origin as reap_idle - anything that reap_idle thinks should be kept
             Session, busy is reported here. Two criterion drifts will cause Control Plane to recycle the Runtime itself
             Conversations that are considered alive are the most difficult type of inconsistency to check."""
        current = now if now is not None else time.monotonic()
        with self._lock:
            sessions = list(self._sessions.values())
        running = sum(1 for session in sessions if session.running)
        idle_seconds = min(
            (current - session.last_activity for session in sessions),
            default=None,
        )
        busy = running > 0 or (
            idle_seconds is not None and idle_seconds < self.idle_ttl_seconds
        )
        return {
            "active_sessions": len(sessions),
            "running_commands": running,
            "idle_seconds": idle_seconds,
            "busy": busy,
        }

    def reap_idle(self, now: float | None = None) -> int:
        """Reclaim slots from idle sessions and from runaway commands.

        Idle TTL alone never frees a session whose command does not end:
        ``running`` only clears on the completion marker, so 16 unattended
        ``sleep infinity`` calls permanently burn every slot of a Pod whose TTL
        keeps being refreshed by the next proxied request.
        """
        current = now if now is not None else time.monotonic()
        expired: list[ShellSession] = []
        idle: list[ShellSession] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                started = session.command_started_at
                if session.running:
                    if (
                        started is not None
                        and current - started > self.max_wall_seconds
                    ):
                        expired.append(self._sessions.pop(session_id))
                    continue
                if current - session.last_activity >= self.idle_ttl_seconds:
                    idle.append(self._sessions.pop(session_id))
        for session in expired:
            session.expire()
        for session in idle:
            session.close()
        return len(expired) + len(idle)

    def _new_session(self, session_id: str) -> ShellSession:
        return ShellSession(
            session_id,
            self.workspace,
            self.env,
            max_output_chars=self.max_output_chars,
        )

    def get_or_create(self, session_id: str) -> tuple[ShellSession, bool]:
        """Retrieve or create a new session; SessionCapacityError will be thrown when the quota is full and all are running.

        🔴 Constraint: **Never call session.close()** while holding self._lock. budget for close()
             is KILL_REAP_SECONDS(10s) + READER_JOIN_SECONDS(5s), while
             activity_snapshot takes the same lock, and the Control Plane's activity probe only waits for 2 seconds.
             When the probe times out, press the AI-LOCK on its side and it will be judged as "deletable" - so one elimination can make the probe
             The working Runtime is deleted along with all sessions.
             Actual measurement (2026-08-19): Make wait() of the killed process 3 seconds slower (gVisor + 500m
             KILL_REAP comment describes exactly this condition), activity_snapshot takes 2.60s,
             The 2 second probe line has been crossed.
             reap_idle / cancel / handle(kill) / close is originally "removing the table inside the lock and outside the lock"
             "Collect corpses", this was the only exception.

        Boundary: Once the session pops out of _sessions, the quota is returned on the spot, and no one else can
             It cannot be addressed - so collecting corpses outside the lock does not relax the upper limit, and there will not be a second thread at the same time
             close the same session. The price is to temporarily hold one more PTY during the elimination period (the corpse collection outside the lock is completed)
             The new session has been created before), which is consistent with the shape of reap_idle batch recycling."""
        stale: ShellSession | None = None
        evicted: ShellSession | None = None
        session: ShellSession
        try:
            with self._lock:
                existing = self._sessions.get(session_id)
                if existing is not None and existing.alive:
                    return existing, False
                if existing is not None:
                    stale = self._sessions.pop(session_id)

                if len(self._sessions) >= self.max_sessions:
                    candidates = [
                        session
                        for session in self._sessions.values()
                        if not session.running
                    ]
                    if not candidates:
                        raise SessionCapacityError(
                            f"all {self.max_sessions} shell sessions are busy"
                        )
                    oldest = min(
                        candidates, key=lambda item: item.last_activity
                    )
                    evicted = self._sessions.pop(oldest.session_id, None)

                #_new_session remains in the lock: its slow path is only the 400 bytes of _write
                #PROMPT_COMMAND writes an empty PTY that has just been opened and is being read by bash.
                #Microsecond level. To move it out, we need to create a window between "check quota" and "occupy quota"
                #Introducing preemption counting is to exchange a real upper limit risk for a theoretical delay.
                session = self._new_session(session_id)
                self._sessions[session_id] = session
        finally:
            #finally instead of sequential execution: throw SessionCapacityError or _new_session
            #When it fails, these sessions have been removed from _sessions. If they are not collected, there will be no one anymore.
            #The referenced bash + PTY persists to the end of the Pod.
            for closing in (evicted, stale):
                if closing is not None:
                    closing.close()
        return session, stale is not None

    def get(self, session_id: str) -> ShellSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return session if session is not None and session.alive else None

    def cancel(self, session_id: str) -> bool:
        """Remove and terminate one session after its transport disconnects."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    def _result_with_restart(
        self,
        result: dict[str, Any],
        restarted: bool,
    ) -> dict[str, Any]:
        return {**result, "session_restarted": restarted}

    def handle(
        self,
        arguments: dict[str, Any],
        on_chunk: Callable[[str, str], None],
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        action = arguments.get("action")
        if action not in SESSION_ACTIONS:
            raise ValueError(
                "action must be session_exec, session_wait, session_input, or session_kill"
            )
        session_id = _validate_session_id(arguments.get("session_id"))
        maximum = (
            self.max_wait_timeout_seconds
            if action == "session_wait"
            else self.max_timeout_seconds
        )
        timeout = _validate_timeout(
            arguments.get("timeout_seconds", self.max_timeout_seconds), maximum
        )

        if action == "session_exec":
            command = arguments.get("command")
            if not isinstance(command, str) or not command:
                raise ValueError("command must be a non-empty string")
            if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
                raise ValueError("command is too large")
            async_return = arguments.get("async", False)
            if not isinstance(async_return, bool):
                raise ValueError("async must be a boolean")
            session, restarted = self.get_or_create(session_id)
            result = session.execute(
                command,
                timeout,
                on_chunk,
                is_cancelled,
                async_return=async_return,
            )
            return self._result_with_restart(result, restarted)

        session = self.get(session_id)
        if session is None:
            raise ValueError(
                f"shell session {session_id!r} does not exist; call exec first"
            )
        if action == "session_wait":
            return self._result_with_restart(
                session.wait(timeout, on_chunk, is_cancelled),
                False,
            )
        if action == "session_input":
            value = arguments.get("input")
            if not isinstance(value, str):
                raise ValueError("input must be a string")
            if len(value.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ValueError("input is too large")
            append_newline = arguments.get("append_newline", True)
            if not isinstance(append_newline, bool):
                raise ValueError("append_newline must be a boolean")
            return self._result_with_restart(
                session.send_input(
                    value,
                    append_newline,
                    timeout,
                    on_chunk,
                    is_cancelled,
                ),
                False,
            )

        session.close()
        with self._lock:
            self._sessions.pop(session_id, None)
        replacement, _ = self.get_or_create(session_id)
        replacement.last_activity = time.monotonic()
        return {
            "action": "kill",
            "session_id": session_id,
            "exit_code": -signal.SIGKILL,
            "stdout": "Session process group terminated; shell restarted.\n",
            "stderr": "",
            "running": False,
            "timed_out": False,
            "wait_timed_out": False,
            "output_truncated": False,
            "session_restarted": True,
        }

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
