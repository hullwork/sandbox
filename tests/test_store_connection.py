"""Connection bounds of the control-plane store and the Control Plane request handler.

Every authenticated request passes STORE.authenticate under one process-wide
lock, so an unbounded statement, a half-open database socket, or a client that
never finishes its request line each turn into "the whole API queues". These
tests pin the bounds that keep that from happening.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_store_module():
    path = ROOT / "control_plane/store.py"
    spec = importlib.util.spec_from_file_location("sandbox_store_for_connection_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


store_module = load_store_module()
Store = store_module.Store
StoreError = store_module.StoreError
connection_hardening = store_module.connection_hardening


def run_control_plane_probe(code: str, **overrides: str | None) -> subprocess.CompletedProcess:
    """Import Control Plane modules in a subprocess with a controlled environment.

    core.py validates configuration at import time and exits the process on
    a missing value, so the only faithful way to observe that gate is a fresh
    interpreter. A ``None`` override removes the variable, so a value present
    in the ambient environment cannot mask the missing case.
    """
    environment = {
        **os.environ,
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
        "SIGNING_KEY": "0" * 32,
        "WORKSPACE_ID_KEY": "1" * 32,
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
        "PYTHONPATH": str(ROOT),
    }
    for name, value in overrides.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


class ConnectionHardeningTests(unittest.TestCase):
    """Per-backend driver kwargs: bounded statements, dead-peer detection."""

    def test_postgresql_bounds_statements_and_pins_isolation(self) -> None:
        kwargs = connection_hardening(
            "postgresql", statement_timeout_ms=5000, idle_tx_timeout_ms=10000
        )
        self.assertIn("-c statement_timeout=5000", kwargs["options"])
        self.assertIn("-c idle_in_transaction_session_timeout=10000", kwargs["options"])
        # claim_workspace/admit_runtime are only correct under READ COMMITTED.
        self.assertIn("-c default_transaction_isolation=read\\ committed", kwargs["options"])
        self.assertEqual(kwargs["keepalives"], 1)
        for name in ("keepalives_idle", "keepalives_interval", "keepalives_count"):
            self.assertGreater(kwargs[name], 0, name)

    def test_mysql_gets_socket_deadlines_and_no_libpq_options(self) -> None:
        # pymysql rejects unknown kwargs with TypeError; only its own names may appear.
        kwargs = connection_hardening(
            "mysql", statement_timeout_ms=1500, idle_tx_timeout_ms=10000
        )
        self.assertEqual(kwargs, {"read_timeout": 2, "write_timeout": 2})

    def test_sqlite_is_left_alone(self) -> None:
        self.assertEqual(
            connection_hardening(
                "sqlite", statement_timeout_ms=5000, idle_tx_timeout_ms=10000
            ),
            {},
        )

    def test_the_kwargs_reach_the_postgres_driver_verbatim(self) -> None:
        seen: dict = {}

        def connect(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("no server in this test")

        store = Store.postgres(
            {
                "host": "db",
                **connection_hardening(
                    "postgresql", statement_timeout_ms=5000, idle_tx_timeout_ms=10000
                ),
            },
            connect=connect,
        )
        with self.assertRaises(StoreError):
            store.ensure_schema()
        self.assertIn("statement_timeout=5000", seen["options"])
        self.assertEqual(seen["keepalives"], 1)

    def test_readiness_checks_the_canonical_schema(self) -> None:
        statements: list[str] = []

        class Cursor:
            def execute(self, statement, params=()):
                statements.append(statement)

        class Connection:
            def cursor(self):
                return Cursor()
            def commit(self):
                pass
            def rollback(self):
                pass
            def close(self):
                pass

        Store.postgres({}, connect=lambda **_kwargs: Connection()).check_ready()
        self.assertEqual(statements, ["SELECT 1 FROM sandbox_tenants WHERE 1 = 0"])

    def test_schema_failure_is_a_hard_startup_failure(self) -> None:
        result = run_control_plane_probe(
            "from control_plane import core; "
            "from control_plane import server; "
            "from control_plane.store import StoreError; "
            "core.STORE = type('BrokenStore', (), {'ensure_schema': lambda self: (_ for _ in ()).throw(StoreError('migration failed'))})(); "
            "server.run_api_server()"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("migration failed", result.stderr)

    def test_disabled_exporter_ignores_an_ambient_grpc_protocol(self) -> None:
        result = run_control_plane_probe(
            "from control_plane import tracing; assert tracing._OTLP_ENDPOINT == ''",
            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=None,
            OTEL_EXPORTER_OTLP_ENDPOINT=None,
            OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=None,
            OTEL_EXPORTER_OTLP_PROTOCOL="grpc",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StoreLockTimeoutTests(unittest.TestCase):
    """The process-wide store lock is waited for, not forever."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.store = Store.sqlite(Path(self._temporary_directory.name) / "store.sqlite3")
        self.store.ensure_schema()
        # Nobody keeps a test that waits the production 10 seconds.
        self.store._lock_timeout = 0.2

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_a_thread_blocked_by_another_holder_gets_database_is_busy(self) -> None:
        holding = threading.Event()
        release = threading.Event()

        def hold_the_lock() -> None:
            with self.store._cursor():
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        try:
            self.assertTrue(holding.wait(5))
            started = time.monotonic()
            with self.assertRaises(StoreError) as caught:
                with self.store._cursor():
                    pass
            self.assertEqual(str(caught.exception), "database is busy")
            # Bounded wait: well under the 5s the holder would otherwise sit there.
            self.assertLess(time.monotonic() - started, 2)
        finally:
            release.set()
            holder.join(5)
        # The failed acquire must not leave the lock in a broken state.
        with self.store._cursor() as cursor:
            cursor.execute("SELECT 1")

    def test_same_thread_reentry_does_not_wait_or_fail(self) -> None:
        # try_lock followed by _cursor is the documented usage; RLock re-entry.
        with self.store.try_lock() as acquired:
            self.assertTrue(acquired)
            started = time.monotonic()
            with self.store._cursor() as cursor:
                cursor.execute("SELECT 1")
            self.assertLess(time.monotonic() - started, 0.2)


class ApiHandlerTimeoutTests(unittest.TestCase):
    """A silent client cannot pin a Control Plane request thread forever."""

    def test_the_handler_declares_a_socket_timeout(self) -> None:
        # socketserver.StreamRequestHandler.setup() calls settimeout(self.timeout)
        # only when it is not None; None means rfile.read() blocks indefinitely.
        result = run_control_plane_probe("from control_plane import api; print(repr(api.ApiHandler.timeout))")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "30")


class WorkspaceIdKeyTests(unittest.TestCase):
    """WORKSPACE_ID_KEY is required for the control_plane role and never falls back."""

    def test_the_control_plane_role_refuses_to_start_without_it(self) -> None:
        result = run_control_plane_probe("from control_plane import core as control_plane", SANDBOX_CONTROL_PLANE_ROLE="api", WORKSPACE_ID_KEY=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required configuration", result.stderr)
        self.assertIn("WORKSPACE_ID_KEY is required", result.stderr)
        self.assertIn("workspace-id-key", result.stderr)

    def test_the_control_plane_role_passes_the_gate_when_it_is_set(self) -> None:
        # Control for the test above: with the key present, whatever stops the
        # import later (no cluster here) is not the configuration gate.
        result = run_control_plane_probe("from control_plane import core as control_plane", SANDBOX_CONTROL_PLANE_ROLE="api")
        self.assertNotIn("missing required configuration", result.stderr)
        self.assertNotIn("WORKSPACE_ID_KEY", result.stderr)

    def test_the_volume_role_does_not_need_it(self) -> None:
        # The volume agent never derives workspace IDs and must not hold key material.
        result = run_control_plane_probe("from control_plane import core as control_plane", WORKSPACE_ID_KEY=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
