"""Never freeze stdout/stderr: bounded queue + single drain thread.

Why it is needed (real failure on 2026-08-17): When the container log consumer is shut down (node jitter,
The runtime log forwarding link is broken), the stdout pipe buffer is full, and the print() of any thread will be permanently
blocked. The observed scene: the agent run thread was printing its completion summary - the log stopped mid-word, the session
stayed stuck in running, the final message never reached the database, yet the HTTP thread was still alive: every probe
green, only the user could see it.

Trade-off: Logs can be lost, but threads cannot. When full, discard the oldest segment and count; the drain thread
The queue is moved to the real stdout (blocking only occurs to it, it is the daemon); the consumer side recovers
Then enter the cumulative number of discards into the next log, so that the "throw away" event itself is visible instead of disappearing silently.

It should only be installed in the permanent service process (server.main); use native scripts and tests directly
stdout - they do not have the "service must be alive" constraint."""
from __future__ import annotations

import atexit
import sys
import threading
import time
from collections import deque
from typing import Any, TextIO


class SafeStdout:
    """Alternative to print()/sys.stdout: write/flush never blocks.

    Queues are bounded by bytes (default 1MiB), not by number of lines - there is no distribution of log line lengths
    Upper bounds, bounding by line count will turn an overly long JSON dump into a backdoor that bypasses the bounds."""

    def __init__(
        self,
        raw: TextIO,
        *,
        max_bytes: int = 1 << 20,
        poll_seconds: float = 0.05,
    ) -> None:
        self._raw = raw
        self._max_bytes = max_bytes
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, int]] = deque()
        self._queued_bytes = 0
        self._dropped_segments = 0
        self._dropped_reported = 0
        self._closed = False
        self._drain = threading.Thread(
            target=self._drain_loop, name="safe-stdout", daemon=True,
        )
        self._drain.start()

    # ---- Write side: The calling thread only touches the memory queue, never fd ----
    def write(self, text: Any) -> int:
        if not text:
            return 0
        data = str(text)
        size = len(data.encode("utf-8", "replace"))
        with self._lock:
            if self._closed:
                return len(data)
            if size > self._max_bytes:
                # A single segment exceeds the entire queue: retaining it is equivalent to throwing away all other logs and giving up directly.
                self._dropped_segments += 1
                return len(data)
            while self._queued_bytes + size > self._max_bytes and self._queue:
                _, dropped_size = self._queue.popleft()
                self._queued_bytes -= dropped_size
                self._dropped_segments += 1
            self._queue.append((data, size))
            self._queued_bytes += size
        return len(data)

    def flush(self) -> None:
        # flush semantics (wait for data to arrive downstream) cannot be honored here without blocking; print(flush=True)
        # The real intention is "don't save it", that is the drain thread's business. The empty implementation is intentional.
        return None

    # ---- Read side: Observation port for troubleshooting, does not participate in the write path ----
    @property
    def dropped_segments(self) -> int:
        with self._lock:
            return self._dropped_segments

    def pending_segments(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain(self, timeout: float = 2.0) -> None:
        """Block until the queue is empty, then flush the real stream.

        The drain thread below is a daemon, so at interpreter exit it is stopped
        wherever it happens to be and everything still queued goes with it. That
        silently eats the last thing a process ever writes, which is the line
        explaining why it is exiting. Measured in a consumer of this module:
        a server refusing to start on a short identity salt wrote its reason,
        exited 1, and produced zero bytes on both stdout and stderr -- two
        entirely different refusals were indistinguishable.

        Bounded rather than unbounded: this runs at exit, and a consumer that is
        still blocked must not turn a shutdown into a hang.
        """
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            with self._lock:
                drained = self._closed or (
                    not self._queue
                    and self._dropped_reported == self._dropped_segments
                )
            if drained:
                break
            time.sleep(self._poll_seconds)
        try:
            self._raw.flush()
        except (OSError, ValueError):
            return

    # ---- drain: The only thread allowed to block on fd ----
    def _drain_loop(self) -> None:
        while True:
            with self._lock:
                if self._queue:
                    text, size = self._queue.popleft()
                    self._queued_bytes -= size
                else:
                    text = None
                # Read the drop counter on every pass, not only on idle ones. It
                # used to be read in the else-branch alone -- the branch that
                # leaves text as None, which then sleeps and continues below. So
                # the notice this counter exists for could never be written,
                # while _dropped_reported was advanced anyway: the module's one
                # promise, that discarding is visible rather than silent, was
                # unreachable code. A truncated log looked exactly like a quiet
                # one.
                pending = self._dropped_segments - self._dropped_reported
                if pending:
                    self._dropped_reported = self._dropped_segments
            if text is None and not pending:
                time.sleep(self._poll_seconds)
                continue
            try:
                if text is not None:
                    self._raw.write(text)
                if pending:
                    self._raw.write(
                        "[safe-stdout] dropped "
                        f"{pending} log segments while the consumer was blocked\n"
                    )
                self._raw.flush()
            except (OSError, ValueError):
                # The real stdout is closed (process exiting): stop moving, write side continues to discard
                with self._lock:
                    self._closed = True
                try:
                    self._raw.close()
                except (OSError, ValueError):
                    # The stream may already have been closed by its owner.
                    pass
                return

    # ---- Transparent the rest of the TextIO protocol to the real stream ----
    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def install() -> SafeStdout | None:
    """Replace sys.stdout / sys.stderr with safe versions (idempotent).

    Returns a safe instance of stdout (convenient for testing and observation), returns None when the native stream cannot be replaced
    Rather than preventing the service from starting - log security is a hardening item, not a prerequisite for startup.

    Each proxy installed here is drained at interpreter exit. Without that the
    daemon drain thread is simply stopped and whatever is still queued is lost,
    which is worst for the message that matters most: the one a process writes
    immediately before it exits."""
    marker = "_sandbox_safe_stdout"
    stdout: SafeStdout | None = None
    if sys.stdout is not None and getattr(sys.stdout, marker, False):
        stdout = sys.stdout  # type: ignore[assignment]
    elif sys.stdout is not None and hasattr(sys.stdout, "write"):
        stdout = SafeStdout(sys.stdout)
        setattr(stdout, marker, True)
        sys.stdout = stdout  # type: ignore[assignment]
        atexit.register(stdout.drain)
    if sys.stderr is not None and not getattr(sys.stderr, marker, False):
        if hasattr(sys.stderr, "write"):
            stderr = SafeStdout(sys.stderr)
            setattr(stderr, marker, True)
            sys.stderr = stderr  # type: ignore[assignment]
            atexit.register(stderr.drain)
    return stdout
