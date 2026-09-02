"""Upgrading a database that shipped before the current columns existed.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
without an explicit migration an in-place upgrade leaves the old shape and the
first query naming a new column fails at runtime - on a deployment somebody else
operates, at a moment unrelated to this change.

Two halves are tested because one is not evidence for the other:

* SQLite runs the real thing end to end, on a database physically built with the
  old schema.
* PostgreSQL and MySQL cannot be started here, so their statements are captured
  from a recording connection. That is enough to catch the two backend
  differences that actually bite - `ADD COLUMN IF NOT EXISTS` exists only on
  PostgreSQL, and MySQL rejects a `DEFAULT` on a `TEXT` column - neither of
  which SQLite would ever complain about.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_store_module():
    path = ROOT / "control_plane/store.py"
    spec = importlib.util.spec_from_file_location("sandbox_store_migrations", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


store_module = load_store_module()
Store = store_module.Store

#: The shape these tables had before the authentication rework, written out
#: rather than derived: a migration test whose "old schema" is generated from
#: today's source is testing nothing.
LEGACY_SCHEMA = (
    """
    CREATE TABLE sandbox_tenants (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        max_workspaces INTEGER NOT NULL,
        max_runtimes INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        disabled_at TEXT
    )
    """,
    """
    CREATE TABLE sandbox_api_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        key_prefix TEXT NOT NULL UNIQUE,
        key_sha256 TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE sandbox_workspaces (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        principal_kind TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        deleted_at TEXT,
        PRIMARY KEY (tenant_id, workspace_id)
    )
    """,
    """
    CREATE TABLE sandbox_runtimes (
        tenant_id TEXT NOT NULL,
        sandbox_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, sandbox_id)
    )
    """,
)

PLAINTEXT_KEY = "sk_legacy_acme_written-before-the-columns-existed"


class SqliteUpgradeTests(unittest.TestCase):
    """The real migration, on a database built with the old schema."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._directory.name) / "legacy.sqlite3"
        with sqlite3.connect(self.path) as connection:
            for statement in LEGACY_SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO sandbox_tenants "
                "(id, display_name, max_workspaces, max_runtimes) "
                "VALUES ('acme', 'Acme', 4, 4)"
            )
            connection.execute(
                "INSERT INTO sandbox_api_keys "
                "(id, tenant_id, key_prefix, key_sha256, label) "
                "VALUES ('0123456789abcdef', 'acme', ?, ?, 'shipped earlier')",
                (PLAINTEXT_KEY[:store_module.KEY_PREFIX_LENGTH],
                 store_module.hash_key(PLAINTEXT_KEY)),
            )
            connection.execute(
                "INSERT INTO sandbox_runtimes "
                "(tenant_id, sandbox_id, workspace_id, template_id, status) "
                "VALUES ('acme', 'sb-00000000000a', 'ws-00000000000a', "
                "'default', 'active')"
            )
            connection.execute(
                "INSERT INTO sandbox_workspaces "
                "(tenant_id, workspace_id, principal_kind, principal_id, session_key) "
                "VALUES ('acme', 'ws-00000000000a', 'service', 'default', 's-1')"
            )
        self.store = Store.sqlite(self.path)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def columns(self, table: str) -> set[str]:
        with sqlite3.connect(self.path) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_the_old_schema_really_lacks_the_columns(self) -> None:
        # Guards the fixture: if this ever passes by accident, every assertion
        # below would be checking a database that never needed migrating.
        self.assertNotIn("permissions", self.columns("sandbox_api_keys"))
        self.assertNotIn("expires_at", self.columns("sandbox_api_keys"))
        self.assertNotIn("token_epoch", self.columns("sandbox_runtimes"))
        self.assertNotIn("token_epoch", self.columns("sandbox_workspaces"))

    def test_ensure_schema_adds_every_missing_column(self) -> None:
        self.store.ensure_schema()
        self.assertLessEqual(
            {"permissions", "expires_at"}, self.columns("sandbox_api_keys")
        )
        self.assertIn("token_epoch", self.columns("sandbox_runtimes"))
        self.assertIn("token_epoch", self.columns("sandbox_workspaces"))

    def test_rows_written_before_the_migration_keep_working(self) -> None:
        self.store.ensure_schema()
        key = self.store.authenticate(PLAINTEXT_KEY)
        self.assertIsNotNone(key)
        assert key is not None
        self.assertEqual(key.tenant_id, "acme")
        # Defaults, not NULL: an existing key must not come back as expired, and
        # must not silently acquire a permission it was never issued.
        self.assertEqual(key.permissions, frozenset())
        self.assertIsNone(key.expires_at)
        self.assertFalse(key.may_act_as_subjects)
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 1)
        self.assertEqual(self.store.workspace_epoch("ws-00000000000a"), 1)

    def test_the_migrated_columns_are_usable_afterwards(self) -> None:
        self.store.ensure_schema()
        self.assertEqual(self.store.bump_runtime_epoch("sb-00000000000a"), 2)
        plaintext, record = self.store.issue_api_key(
            "acme", "after", permissions=["act_as_subjects"], expires_in_seconds=60
        )
        authenticated = self.store.authenticate(plaintext)
        assert authenticated is not None
        self.assertTrue(authenticated.may_act_as_subjects)
        self.assertEqual(authenticated.expires_at, record.expires_at)

    def test_running_it_again_changes_nothing(self) -> None:
        self.store.ensure_schema()
        before = {table: self.columns(table) for table in (
            "sandbox_api_keys", "sandbox_runtimes", "sandbox_workspaces"
        )}
        self.store.bump_runtime_epoch("sb-00000000000a")
        self.store.ensure_schema()
        self.store.ensure_schema()
        after = {table: self.columns(table) for table in before}
        self.assertEqual(before, after)
        # An ALTER re-applied would have reset the row to its default.
        self.assertEqual(self.store.runtime_epoch("sb-00000000000a"), 2)

    def test_a_current_database_needs_no_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fresh = Store.sqlite(pathlib.Path(directory) / "fresh.sqlite3")
            fresh.ensure_schema()
            fresh.ensure_schema()
            fresh.create_tenant("acme", "Acme", max_workspaces=1, max_runtimes=1)
            plaintext, _ = fresh.issue_api_key("acme", "k")
            self.assertIsNotNone(fresh.authenticate(plaintext))


class _RecordingCursor:
    """Answers "the new columns are absent" and records every statement."""

    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log
        self._rows: list[tuple] = []

    def execute(self, statement, parameters=()):
        self.log.append((" ".join(str(statement).split()), tuple(parameters)))
        upper = statement.upper()
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            # Deliberately not naming the new columns: this is the pre-upgrade
            # database, which is the only interesting case.
            self._rows = [("id",), ("tenant_id",)]
        elif "INFORMATION_SCHEMA.STATISTICS" in upper:
            self._rows = [(1,)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingConnection:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log

    def cursor(self):
        return _RecordingCursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class ServerBackendStatementTests(unittest.TestCase):
    """What the two server backends are actually sent."""

    def statements(self, dialect) -> list[tuple[str, tuple]]:
        log: list[tuple[str, tuple]] = []
        Store(dialect, lambda: _RecordingConnection(log)).ensure_schema()
        return log

    def alters(self, dialect) -> list[str]:
        return [
            statement for statement, _ in self.statements(dialect)
            if statement.startswith("ALTER TABLE")
        ]

    def test_each_backend_is_asked_which_columns_exist(self) -> None:
        for dialect, scope in (
            (store_module._POSTGRES, "current_schema()"),
            (store_module._MYSQL, "DATABASE()"),
        ):
            with self.subTest(backend=dialect.name):
                lookups = [
                    (statement, parameters)
                    for statement, parameters in self.statements(dialect)
                    if "information_schema.columns" in statement
                ]
                self.assertEqual(len(lookups), 3, lookups)
                self.assertIn(scope, lookups[0][0])
                self.assertEqual(
                    {parameters[0] for _, parameters in lookups},
                    {"sandbox_api_keys", "sandbox_runtimes", "sandbox_workspaces"},
                )

    def test_every_missing_column_is_added_on_both_backends(self) -> None:
        for dialect in (store_module._POSTGRES, store_module._MYSQL):
            with self.subTest(backend=dialect.name):
                alters = self.alters(dialect)
                self.assertEqual(len(alters), len(store_module._COLUMN_MIGRATIONS))
                for table, column, _ in store_module._COLUMN_MIGRATIONS:
                    self.assertTrue(
                        any(f"ALTER TABLE {table} ADD COLUMN {column} " in statement
                            for statement in alters),
                        f"{dialect.name}: {table}.{column} not added",
                    )

    def test_mysql_never_defaults_a_text_column(self) -> None:
        # 🔴 MySQL rejects `TEXT NOT NULL DEFAULT ''` outright. The dialect
        # rewrite to VARCHAR is what makes the shared statement legal there, and
        # SQLite and PostgreSQL would both accept the broken spelling happily.
        permissions = next(
            statement for statement in self.alters(store_module._MYSQL)
            if "permissions" in statement
        )
        self.assertIn("VARCHAR(256) NOT NULL DEFAULT ''", permissions)
        self.assertNotIn("TEXT", permissions)
        postgres = next(
            statement for statement in self.alters(store_module._POSTGRES)
            if "permissions" in statement
        )
        self.assertIn("TEXT NOT NULL DEFAULT ''", postgres)

    def test_the_postgresql_only_spelling_is_never_sent(self) -> None:
        # `ADD COLUMN IF NOT EXISTS` reads as portable and is not: MySQL and
        # SQLite both reject it, and the failure lands during someone's upgrade.
        # Asserted against what is executed rather than against the source text,
        # so the comment explaining the trap does not trip its own guard.
        for dialect in (store_module._POSTGRES, store_module._MYSQL):
            with self.subTest(backend=dialect.name):
                offenders = [
                    statement for statement in self.alters(dialect)
                    if "IF NOT EXISTS" in statement.upper()
                ]
                self.assertEqual(offenders, [], dialect.name)

    def test_no_check_constraint_is_introduced(self) -> None:
        # PostgreSQL regex CHECK syntax does not even create the table on
        # SQLite, so a constraint written once guards one backend in three.
        for _, _, definition in store_module._COLUMN_MIGRATIONS:
            self.assertNotIn("CHECK", definition.upper())


if __name__ == "__main__":
    unittest.main()
