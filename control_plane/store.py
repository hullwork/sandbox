"""Relational data of the Sandbox control plane: tenant, API key, Workspace ownership.

PostgreSQL is the production backend. SQLite keeps local development and unit
tests self-contained. Statements are shared templates rendered through a small
dialect layer so backend behavior cannot drift through duplicated queries.

Responsibilities: only "who owns what and who may do what" is stored. Workspace contents are not
     (they live on the volume), nor is Runtime running state (that lives in Kubernetes).
Constraints: every primary key starts with tenant_id, so "missing tenant" is unrepresentable at the
     SQL layer rather than something every query has to remember to filter out with WHERE."""
from __future__ import annotations

import contextlib
import datetime
import time
import hashlib
import hmac
import os
import importlib
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


class StoreError(RuntimeError):
    """The database is unavailable, or a control plane write was rejected."""


TENANT_ID = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")
# Principal and session are defined by the tenants themselves; the Control Plane does not interpret them. This only
# rejects what would break key derivation and logging (control characters, overlong values).
FREEFORM = re.compile(r"^[^\x00-\x1f]{1,128}$")
KEY_PREFIX_LENGTH = 12
# Literally identical to control_plane.TEMPLATE_ID. The Control Plane is a separate service and the store does not import it, so
# the rule is written in two places; tests/test_sandbox_store.py has a test case that ties the two together.
TEMPLATE_ID = re.compile(r"^[a-z0-9][-a-z0-9]{0,31}$")
# The tenant segment of an object owner. **Intentionally wider than TENANT_ID**: it comes from the caller's
# application-layer identity (RuntimeIdentity.tenant_id), allows mixed case, dot, @, underscore and up to 128
# characters - verbatim the single-segment rule of control_plane.OBJECT_OWNER. Narrowing it to TENANT_ID would make
# legitimate owners unregistrable, leaving operators no choice but to abandon this gate entirely.
OWNER_TENANT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
# Ownership value of a global template: visible to all tenants.
#
# A literal '*' rather than NULL: NULL inside a composite primary key behaves differently on the two backends -
# PostgreSQL allows multiple NULL rows, so (NULL, 'python-ml') could be inserted repeatedly and the primary key
# would be meaningless; SQLite instead treats NULL as a value. This literal is part of the cross-service contract
# (the Console uses it to tell global templates from tenant templates); changing it means changing the contract too.
GLOBAL_TENANT = "*"
# Runtime records that belong to no tenant hang under this key: sandboxes created by the global management-plane
# identity (admin key without X-Sandbox-Tenant) or by the static SANDBOX_CONTROL_PLANE_TOKEN.
#
# It is not a tenant; it only gives "how far has this sandbox been provisioned" a place to live. A literal
# rather than NULL for the same reason as GLOBAL_TENANT (tenant_id is part of the composite primary key of
# sandbox_runtimes). '-' cannot collide with a real tenant name: TENANT_ID requires the first character to be [a-z0-9].
UNTENANTED_RUNTIME = "-"
# The reserved tenant row the unscoped management-plane identity is filed under
# (ApiHandler.ensure_management_tenant). It matches TENANT_ID on purpose - it
# is a real row so capability epochs can be revoked - so the name has to be
# refused everywhere a caller could pick a tenant id: create_tenant,
# issue_api_key, X-Sandbox-Tenant / session tenant (_assume_tenant) and the
# OIDC tenant claim (oidc.role_of). Without that, whoever registers a tenant
# called "management" owns every workspace and runtime the management plane
# created, and can shrink the management plane's quota to any value.
MANAGEMENT_TENANT = "management"
# The minimum **shape** of an image reference. The real admission rule is the Control Plane's prefix whitelist, which is
# only known at deployment time (environment variables); the store layer does not repeat it and only rejects the
# bytes that would corrupt the Pod spec and logs - same reasoning as FREEFORM. The upper bound is 256: a
# reference with a digest can exceed 200 characters.
IMAGE_REF = re.compile(r"^[^\x00-\x1f]{1,256}$")

# The three outcomes of Store.admit_workspace. Named constants rather than bools: there are three endings, and
# two of them ("took a new quota slot" and "reused an existing row") behave **in opposite ways** when a rollback
# fails - expressing them as True/False hides that difference from the call site.
#: A new quota slot was taken and a row was written.
WORKSPACE_ADMITTED = "admitted"
#: The same Workspace of the same tenant is already recorded (idempotent re-entry); only last_used_at is refreshed.
WORKSPACE_REUSED = "reused"
#: The conditional insert affected 0 rows: a concurrent request took the last slot. Neither a success nor an error.
WORKSPACE_AT_CAPACITY = "at_capacity"


# Write-throttling window for last_used_at.
#
# The question the column answers is "is this key still in use?" - it decides whether a tenant key can be
# revoked, and who is still hanging on the static SANDBOX_CONTROL_PLANE_TOKEN. That question needs "days" of accuracy, not "seconds".
#
# 🔴 The cost of not throttling is not wasted writes but **the column degenerating into a constant now()**: an open
# console polls two endpoints every 5 seconds, and every request passes through _assume_tenant, so "recently used"
# is always "just now" and a truly idle key looks exactly like one used daily. Expecting every caller to hold back
# on its own is unrealistic - polling on the agent host side cannot be controlled, only the write entry point can.
TOUCH_THROTTLE_SECONDS = 300


@dataclass(frozen=True)
class _Dialect:
    name: str
    placeholder: str
    now: str
    timestamp_type: str
    #: Expression for "now minus the throttling window". There is **no** shared spelling of date arithmetic across
    #: the backends: SQLite does not understand PostgreSQL's INTERVAL literal, and PostgreSQL does not understand
    #: SQLite's datetime() modifier. Differences of this kind are the reason _Dialect exists.
    stale_cutoff: str
    #: Row-lock suffix. Quota admission must put "count once" and "take one" into the same serialization, otherwise
    #: two concurrent requests both count N-1 and both insert. PostgreSQL relies on SELECT ... FOR UPDATE to lock
    #: the tenant row; SQLite has no such syntax and needs none - its write transaction is already database-level
    #: serial. Both end up with "admissions for the same tenant are mutually exclusive", by different mechanisms.
    row_lock: str
    #: "Now minus {ph} seconds". Unlike stale_cutoff the seconds are supplied by the caller rather than being the
    #: hard-coded throttle window. Date arithmetic has no shared spelling across backends, hence another dialect field.
    age_cutoff: str

    def render(self, template: str) -> str:
        rendered = template.format(
            ph=self.placeholder,
            now=self.now,
            ts_type=self.timestamp_type,
            stale=self.stale_cutoff,
            row_lock=self.row_lock,
            age_cutoff=self.age_cutoff.format(ph=self.placeholder),
        )
        if self.name == "mysql":
            # MySQL cannot index a bare TEXT column. 256 characters covers all
            # identifiers and image references validated by this store.
            rendered = re.sub(r"\bTEXT\b", "VARCHAR(256)", rendered)
        return rendered


_POSTGRES = _Dialect(
    name="postgresql",
    placeholder="%s",
    now="NOW()",
    timestamp_type="TIMESTAMPTZ",
    stale_cutoff=f"NOW() - INTERVAL '{TOUCH_THROTTLE_SECONDS} seconds'",
    row_lock="FOR UPDATE",
    age_cutoff="NOW() - ({ph} * INTERVAL '1 second')",
)

_MYSQL = _Dialect(
    name="mysql",
    placeholder="%s",
    now="(UTC_TIMESTAMP(6))",
    timestamp_type="DATETIME(6)",
    stale_cutoff=(
        f"(UTC_TIMESTAMP(6) - INTERVAL '{TOUCH_THROTTLE_SECONDS}' SECOND)"
    ),
    row_lock="FOR UPDATE",
    age_cutoff="(UTC_TIMESTAMP(6) - INTERVAL {ph} SECOND)",
)

# SQLite stores timestamps as TEXT and returns them as strings rather than datetime. Callers always treat the
# value as opaque and pass it through unchanged, so the backends look the same from outside.
# SQLite's CURRENT_TIMESTAMP and datetime() both produce UTC 'YYYY-MM-DD HH:MM:SS'; that format sorts
# lexicographically in time order, so `<` on the TEXT column is correct.
_SQLITE = _Dialect(
    name="sqlite",
    placeholder="?",
    now="CURRENT_TIMESTAMP",
    timestamp_type="TEXT",
    stale_cutoff=f"datetime('now', '-{TOUCH_THROTTLE_SECONDS} seconds')",
    row_lock="",
    age_cutoff="datetime('now', '-' || {ph} || ' seconds')",
)


_SCHEMA_TEMPLATES = (
    """
    CREATE TABLE IF NOT EXISTS sandbox_tenants (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        max_workspaces INTEGER NOT NULL,
        max_runtimes INTEGER NOT NULL,
        created_at {ts_type} NOT NULL DEFAULT {now},
        disabled_at {ts_type}
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sandbox_api_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        key_prefix TEXT NOT NULL UNIQUE,
        key_sha256 TEXT NOT NULL,
        label TEXT NOT NULL,
        permissions TEXT NOT NULL DEFAULT '',
        expires_at BIGINT,
        created_at {ts_type} NOT NULL DEFAULT {now},
        last_used_at {ts_type},
        revoked_at {ts_type}
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sandbox_workspaces (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        principal_kind TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        token_epoch BIGINT NOT NULL DEFAULT 1,
        created_at {ts_type} NOT NULL DEFAULT {now},
        last_used_at {ts_type} NOT NULL DEFAULT {now},
        deleted_at {ts_type},
        PRIMARY KEY (tenant_id, workspace_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sandbox_workspaces_principal_idx
    ON sandbox_workspaces (tenant_id, principal_kind, principal_id)
    """,
    # workspace_id is globally unique (HMAC-derived); this index turns "who owns this workspace" into a
    # point lookup - the authentication path asks it on every request.
    """
    CREATE INDEX IF NOT EXISTS sandbox_workspaces_lookup_idx
    ON sandbox_workspaces (workspace_id)
    """,
    # Data model: one row = one Runtime lifecycle record.
    #
    # 🔴 Before this table existed the Runtime had no row in the store at all; its state was "does that Pod exist in
    # K8s?". That is a self-consistent simplification for a single replica (one copy fewer to reconcile), but it is
    # the root cause of three things being impossible at once: per-tenant quota could only count Pod labels, the
    # reaper could only rely on annotations, and concurrent admission could only rely on an in-process lock - which
    # is no lock at all once the Control Plane runs more than one replica.
    #
    # Runtime lifecycle has four states; this project does not implement hibernation:
    #   pending   quota slot taken, Pod not ready yet
    #   active    Pod ready
    #   released  terminal state, quota slot returned
    #   failed    terminal state, failed during provisioning; kept apart from released so the failure rate can be queried
    #
    # 🔴 Status writes always use a conditional update (... WHERE status = <expected>) and check the affected row
    # count. Zero rows **is neither a success nor an error**: it means "a concurrent request changed it first", and
    # the caller must branch on it explicitly. An unconditional update could overwrite a concurrent terminal
    # state and cause reconciliation to treat valid data as abandoned.
    """
    CREATE TABLE IF NOT EXISTS sandbox_runtimes (
        tenant_id TEXT NOT NULL,
        sandbox_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        status TEXT NOT NULL,
        token_epoch BIGINT NOT NULL DEFAULT 1,
        created_at {ts_type} NOT NULL DEFAULT {now},
        updated_at {ts_type} NOT NULL DEFAULT {now},
        PRIMARY KEY (tenant_id, sandbox_id)
    )
    """,
    # Quota admission asks this on every create: "how many live runtimes does this tenant have?"
    """
    CREATE INDEX IF NOT EXISTS sandbox_runtimes_live_idx
    ON sandbox_runtimes (tenant_id, status)
    """,
    # sandbox_id is globally unique (randomly generated). The authentication path looks ownership up by it, so that
    # must be a point lookup - this index replaces the "read the Pod label, then check workspace ownership" hop.
    """
    CREATE INDEX IF NOT EXISTS sandbox_runtimes_lookup_idx
    ON sandbox_runtimes (sandbox_id)
    """,
    # Data model: one row = one control-plane action worth leaving a trace of.
    #
    # Only three categories are recorded, not every request - a full audit would dwarf the business data in volume,
    # and only three questions actually need answering:
    #   1. Management-plane writes (create tenant / issue and revoke keys / change tenant status / add and delete
    #      templates): who handed out which permission, and who took it back?
    #   2. The management plane acting on behalf of a tenant (admin key with X-Sandbox-Tenant): cross-tenant actions
    #      must leave a trace, otherwise "whose data did the administrator look at" cannot be answered.
    #   3. Ownership check rejections: a steady stream of them means someone is probing IDs - a precursor to an attack,
    #      not noise.
    #
    # 🔴 actor_id records a sentinel value for the static SANDBOX_CONTROL_PLANE_TOKEN. The token itself cannot say who is using it;
    # that is why it must be retired, and until it is, the audit must at least show that "it did this".
    #
    # 🔴 Both outcome and action are **closed enumerations**, enforced on the Control Plane side. No CHECK constraint here,
    # for the same reason as the sandbox_templates table (PostgreSQL regex syntax fails to create the table on SQLite).
    """
    CREATE TABLE IF NOT EXISTS sandbox_audit_log (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        actor_kind TEXT NOT NULL,
        actor_id TEXT,
        action TEXT NOT NULL,
        target TEXT,
        outcome TEXT NOT NULL,
        created_at {ts_type} NOT NULL DEFAULT {now}
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sandbox_audit_log_time_idx
    ON sandbox_audit_log (created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS sandbox_audit_log_tenant_idx
    ON sandbox_audit_log (tenant_id, created_at)
    """,
    # Data model: a template is one template_id → image registration.
    #
    # 🔴 Every row of this table is "an image allowed to run in the cluster". Only admin may write it, and the
    # image must fall inside the prefix whitelist fixed at deployment (that rule lives in the Control Plane, not here) -
    # if tenants could name their own images, sandbox isolation would be worthless.
    #
    # tenant_id = '*' marks a global template; see GLOBAL_TENANT for why.
    # created_by records the key_id that made the write (the static SANDBOX_CONTROL_PLANE_TOKEN is recorded as a fixed
    # sentinel value) so that a bad image can be traced back to whoever registered it.
    # deleted_at is a soft delete, consistent with the three tables above: the row stays so that an audit can
    # still answer "which image did this id once point to".
    #
    # Deliberately **no CHECK constraints**: the `CHECK (id ~ '...')` from the design draft is PostgreSQL-only
    # regex syntax; SQLite cannot even create the table (`near "~": syntax error`), so the same DDL would only
    # work on one backend. Validation is always done in Python, with one set of rules shared by both backends.
    """
    CREATE TABLE IF NOT EXISTS sandbox_templates (
        tenant_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        image TEXT NOT NULL,
        created_at {ts_type} NOT NULL DEFAULT {now},
        created_by TEXT,
        deleted_at {ts_type},
        PRIMARY KEY (tenant_id, template_id)
    )
    """,
    # Which owner tenant segments each accessing party (a Control Plane tenant) is allowed to act for.
    #
    # Why this table exists: the owner in an object key has the shape `<tenant>/<subject>`, and its tenant segment
    # comes from the caller's **application-layer** identity (sandbox_client.object_owner spells it out of
    # RuntimeIdentity). That and sandbox_tenants.id (the accessing party) are two separate namespaces - one accessing
    # party having several application-layer tenants is normal usage, so the two cannot be required to be equal.
    # But it cannot be ignored either: without a check, any tenant key could sign a ticket for any owner and write
    # into someone else's object prefix.
    """
    CREATE TABLE IF NOT EXISTS sandbox_owner_prefixes (
        tenant_id TEXT NOT NULL,
        owner_tenant TEXT NOT NULL,
        created_at {ts_type} NOT NULL DEFAULT {now},
        created_by TEXT,
        PRIMARY KEY (tenant_id, owner_tenant)
    )
    """,
)


#: Columns added to tables that shipped without them.
#:
#: 🔴 `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
#: so a deployment that upgrades in place keeps the old shape and every SELECT
#: naming a new column fails at runtime. "No backward compatibility" is about
#: not carrying old *call paths*; handing an operator a database that breaks on
#: upgrade is a different thing, and this product is meant to be run by people
#: who did not write it.
#:
#: Each entry is applied only when the column is absent, so `ensure_schema` stays
#: idempotent and starting an already-migrated deployment is a no-op.
#:
#: 🔴 No CHECK constraints here, for the same reason the CREATE TABLE statements
#: have none: PostgreSQL regex syntax will not create on SQLite, so a constraint
#: written once would silently guard one backend out of three. Validation lives
#: in Python, where all three backends share it.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("sandbox_api_keys", "permissions", "TEXT NOT NULL DEFAULT ''"),
    ("sandbox_api_keys", "expires_at", "BIGINT"),
    ("sandbox_runtimes", "token_epoch", "BIGINT NOT NULL DEFAULT 1"),
    ("sandbox_workspaces", "token_epoch", "BIGINT NOT NULL DEFAULT 1"),
)


@dataclass(frozen=True)
class Tenant:
    id: str
    display_name: str
    status: str
    max_workspaces: int
    max_runtimes: int

    @property
    def active(self) -> bool:
        return self.status == "active"


#: The only permission a key may carry beyond what its scope already grants.
#: A closed vocabulary: an unknown string in this column must be a rejected
#: write, never a permission that quietly does nothing.
ACT_AS_SUBJECTS = "act_as_subjects"
KEY_PERMISSIONS = frozenset({ACT_AS_SUBJECTS})


@dataclass(frozen=True)
class ApiKey:
    id: str
    tenant_id: str | None
    label: str
    permissions: frozenset[str] = frozenset()
    #: Unix seconds, or None for a key that never expires. Deliberately not a
    #: {ts_type} column: the three backends have no shared spelling for date
    #: arithmetic, and the comparison this drives is one Python integer compare
    #: on a row that has already been fetched.
    expires_at: int | None = None

    @property
    def is_admin(self) -> bool:
        """A key with no tenant_id is a management-plane key.

        It can create tenants, issue tenant keys, and act on behalf of any tenant (via X-Sandbox-Tenant)."""
        return self.tenant_id is None

    @property
    def may_act_as_subjects(self) -> bool:
        """Whether this credential may name a pseudonymous subject it acts for.

        🔴 A property of the calling identity, exactly as impersonation is in
        Kubernetes - not a deployment-wide switch. A key without it that sends
        the header is refused, never quietly downgraded to "acting as itself":
        the caller believed it was writing on someone's behalf and needs to be
        told it was not."""
        return ACT_AS_SUBJECTS in self.permissions


#: backend name -> (importable module, PyPI distribution named in the error text).
_DRIVER_MODULES = {
    "postgresql": ("psycopg", "psycopg"),
    "mysql": ("pymysql", "PyMySQL"),
}


def require_driver(backend: str) -> None:
    """Import the database driver for ``backend`` now, or raise StoreError.

    A missing driver is a build defect (the image shipped without the package),
    not a transient outage. The composition root calls this once at startup and
    turns the error into a hard exit, so it can never be mistaken for the
    "store unavailable, requests will return 503" warning that covers
    connectivity problems. The first MySQL overlay shipped exactly that way:
    /readyz was green while every tenant request failed with 503.
    """
    module_name, distribution = _DRIVER_MODULES[backend]
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        raise StoreError(
            f"{backend} driver is unavailable: Python package {distribution!r} "
            f"is not installed ({exc}); add it to control_plane/requirements.lock and "
            "rebuild the Control Plane image"
        ) from exc


def _default_postgres_connect(**kwargs: Any) -> Any:
    require_driver("postgresql")
    import psycopg

    return psycopg.connect(**kwargs)


def _default_mysql_connect(**kwargs: Any) -> Any:
    require_driver("mysql")
    import pymysql

    kwargs.setdefault("charset", "utf8mb4")
    kwargs.setdefault("autocommit", False)
    kwargs.setdefault(
        "init_command", "SET time_zone = '+00:00'"
    )
    connection = pymysql.connect(**kwargs)
    # InnoDB defaults to REPEATABLE READ; claim_workspace/admit_runtime need
    # READ COMMITTED (see the isolation note there), so state it per session.
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    return connection


def _default_sqlite_connect(path: str, timeout: float) -> Any:
    import sqlite3

    connection = sqlite3.connect(path, timeout=timeout)
    # Neither foreign keys nor WAL is on by default; without WAL, concurrent reads and writes occasionally hit
    # "database is locked" in tests.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def connection_hardening(
    backend: str, *, statement_timeout_ms: int, idle_tx_timeout_ms: int
) -> dict[str, Any]:
    """Driver kwargs that bound every statement and notice a dead peer.

    Lives next to the connectors because each driver spells the intent
    differently and an unknown kwarg is a TypeError at connect time: psycopg
    takes libpq ``options`` and TCP keepalives, pymysql only has socket
    read/write deadlines (isolation is set in _default_mysql_connect), sqlite3
    has neither (its ``timeout`` is the busy handler, set by Store.sqlite)."""
    if backend == "postgresql":
        return {
            "options": (
                f"-c statement_timeout={statement_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={idle_tx_timeout_ms} "
                "-c default_transaction_isolation=read\\ committed"
            ),
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    if backend == "mysql":
        seconds = max(1, -(-statement_timeout_ms // 1000))
        return {"read_timeout": seconds, "write_timeout": seconds}
    return {}


# Bound on waiting for the process-wide store lock; past it the request gets
# a 503 instead of queueing behind a transaction that is itself waiting.
LOCK_TIMEOUT_SECONDS = float(os.getenv("SANDBOX_STORE_LOCK_TIMEOUT_SECONDS", "10"))


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key(tenant_id: str | None) -> str:
    """Issue a new key whose stored prefix starts with random material.

    The plaintext exists only at issuance; the database retains its SHA-256.
    Random material must precede the tenant identifier: with the tenant first,
    two tenants sharing a long ID prefix would produce the same 12-character
    lookup prefix and conflict on the unique index. The tenant remains present
    in the full key for operator attribution.
    """
    scope = tenant_id or "admin"
    return f"sk_{secrets.token_urlsafe(9)}_{scope}_{secrets.token_urlsafe(32)}"


class Store:
    """Read and write tenant, API key and Workspace ownership."""

    def __init__(self, dialect: _Dialect, open_connection: Callable[[], Any]):
        self._dialect = dialect
        self._open_connection = open_connection
        self._lock = threading.RLock()
        self._lock_timeout = LOCK_TIMEOUT_SECONDS

    @classmethod
    def postgres(
        cls,
        connect_kwargs: dict[str, Any],
        *,
        connect: Callable[..., Any] | None = None,
    ) -> "Store":
        connector = connect or _default_postgres_connect
        return cls(_POSTGRES, lambda: connector(**connect_kwargs))

    @classmethod
    def mysql(
        cls,
        connect_kwargs: dict[str, Any],
        *,
        connect: Callable[..., Any] | None = None,
    ) -> "Store":
        connector = connect or _default_mysql_connect
        return cls(_MYSQL, lambda: connector(**connect_kwargs))

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        timeout: float = 5.0,
        connect: Callable[..., Any] | None = None,
    ) -> "Store":
        connector = connect or _default_sqlite_connect
        return cls(_SQLITE, lambda: connector(str(path), timeout))

    @property
    def backend(self) -> str:
        return self._dialect.name

    def _sql(self, template: str) -> str:
        return self._dialect.render(template)

    @contextlib.contextmanager
    def try_lock(self) -> Iterator[bool]:
        """Take the store lock without blocking; if it cannot be taken, yield False to the caller instead of waiting.

        Responsibility: only lets the caller ask "can I get in right now?"; opens no connection and sends no statement.
        It exists for one purpose: so that the **unauthenticated, anyone-can-trigger** read-only bypass (the two
             /metrics gauges) backs off immediately when the store is busy instead of queueing in _cursor(). That
             lock is process-wide and is held across connection establishment (PostgreSQL connect_timeout is 5s),
             so one slow store plus a single scraper could hold every authenticated request for seconds.
        🔴 It is an RLock, so once taken, entering _cursor() on the same thread does not self-deadlock - and that is
           the intended usage: take the lock, then count while holding it. Two steps (try-lock, release, then count)
           would be equivalent to no lock at all."""
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[Any]:
        # RLock: same-thread re-entry (try_lock -> _cursor) still succeeds at
        # once; the timeout only bites when another thread holds the lock.
        if not self._lock.acquire(timeout=self._lock_timeout):
            raise StoreError("database is busy")
        try:
            try:
                connection = self._open_connection()
            except Exception as exc:  # drivers do not share a common exception type
                raise StoreError(f"database is unavailable: {exc}") from exc
            try:
                cursor = connection.cursor()
                yield cursor
                connection.commit()
            except StoreError:
                connection.rollback()
                raise
            except Exception as exc:
                connection.rollback()
                raise StoreError(str(exc)) from exc
            finally:
                connection.close()
        finally:
            self._lock.release()

    def ensure_schema(self) -> None:
        with self._cursor() as cursor:
            for template in _SCHEMA_TEMPLATES:
                statement = self._sql(template)
                if self._dialect.name != "mysql":
                    cursor.execute(statement)
                    continue
                index = self._mysql_index(statement)
                if index is None:
                    cursor.execute(statement)
                    continue
                table_name, index_name = index
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = %s AND index_name = %s",
                    (table_name, index_name),
                )
                row = cursor.fetchone()
                if row and int(row[0]) == 0:
                    cursor.execute(statement.replace(" IF NOT EXISTS", "", 1))
            self._add_missing_columns(cursor)

    def check_ready(self) -> None:
        """Verify that the configured database and canonical schema are usable.

        Querying a real schema relation instead of only ``SELECT 1`` catches both
        a dead database and a database that was replaced with an empty one after
        this process completed its startup migration.
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM sandbox_tenants WHERE 1 = 0")

    def _add_missing_columns(self, cursor: Any) -> None:
        """Bring a table that shipped earlier up to the current column set.

        🔴 Deliberately **not** `ADD COLUMN IF NOT EXISTS`: only PostgreSQL
        accepts that clause. MySQL and SQLite both reject it outright, so the
        one spelling that reads as portable would fail on two of the three
        backends - and would fail at upgrade time, on somebody else's
        deployment. Ask which columns exist, then add the ones that do not.
        """
        for table in dict.fromkeys(table for table, _, _ in _COLUMN_MIGRATIONS):
            present = self._existing_columns(cursor, table)
            if not present:
                # The table is absent entirely, which cannot happen after the
                # CREATE statements above unless this store points somewhere
                # unexpected. Adding a column to it would fail anyway; leave the
                # louder error to the first real query.
                continue
            for candidate, column, definition in _COLUMN_MIGRATIONS:
                if candidate != table or column in present:
                    continue
                cursor.execute(
                    self._sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                )

    def _existing_columns(self, cursor: Any, table: str) -> set[str]:
        """Column names of ``table``, or an empty set when it does not exist.

        The table name is interpolated rather than bound: SQLite's PRAGMA takes
        no parameters. Every value reaching here is a literal from
        ``_COLUMN_MIGRATIONS``, never anything a request supplied.
        """
        if self._dialect.name == "sqlite":
            cursor.execute(f"PRAGMA table_info({table})")
            return {str(row[1]) for row in cursor.fetchall()}
        scope = "current_schema()" if self._dialect.name == "postgresql" else "DATABASE()"
        cursor.execute(
            self._sql(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = {scope} AND table_name = {{ph}}"
            ),
            (table,),
        )
        return {str(row[0]) for row in cursor.fetchall()}

    @staticmethod
    def _mysql_index(statement: str) -> tuple[str, str] | None:
        match = re.fullmatch(
            r"\s*CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
            r"([A-Za-z0-9_]+)\s+ON\s+([A-Za-z0-9_]+)\s+.*",
            statement,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        return match.group(2), match.group(1)

    # --- Tenant --------------------------------------------------------------
    def create_tenant(
        self,
        tenant_id: str,
        display_name: str,
        *,
        max_workspaces: int,
        max_runtimes: int,
    ) -> Tenant:
        if not TENANT_ID.fullmatch(tenant_id):
            raise StoreError(
                "tenant id must match ^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$ "
                "(the value is also used as a Kubernetes label)"
            )
        if tenant_id == MANAGEMENT_TENANT:
            raise StoreError(f"tenant id is reserved: {tenant_id}")
        if max_workspaces < 1 or max_runtimes < 1:
            raise StoreError("quotas must be positive")
        return self._insert_tenant(
            tenant_id, display_name, max_workspaces, max_runtimes
        )

    def create_management_tenant(self) -> Tenant:
        """The only way the reserved row gets created; create_tenant refuses the name."""
        return self._insert_tenant(
            MANAGEMENT_TENANT,
            "Reserved management-plane identity",
            1024,
            1024,
        )

    def _insert_tenant(
        self,
        tenant_id: str,
        display_name: str,
        max_workspaces: int,
        max_runtimes: int,
    ) -> Tenant:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_tenants "
                    "(id, display_name, max_workspaces, max_runtimes) "
                    "VALUES ({ph}, {ph}, {ph}, {ph})"
                ),
                (tenant_id, display_name, max_workspaces, max_runtimes),
            )
        return Tenant(
            id=tenant_id,
            display_name=display_name,
            status="active",
            max_workspaces=max_workspaces,
            max_runtimes=max_runtimes,
        )

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT id, display_name, status, max_workspaces, max_runtimes "
                    "FROM sandbox_tenants WHERE id = {ph}"
                ),
                (tenant_id,),
            )
            row = cursor.fetchone()
        return _tenant_from_row(row)

    def list_tenants(self) -> list[Tenant]:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT id, display_name, status, max_workspaces, max_runtimes "
                    "FROM sandbox_tenants ORDER BY id"
                )
            )
            rows = cursor.fetchall()
        return [tenant for tenant in map(_tenant_from_row, rows) if tenant]

    def set_tenant_status(self, tenant_id: str, status: str) -> bool:
        """Change a tenant's status. Returns whether the tenant exists.

        rowcount must be checked: without it an UPDATE that matches no row still "succeeds", and a tenant id with
        one wrong character gets a 200 plus a "suspended successfully" audit entry while zero rows changed.
        The sibling route POST /v1/admin/tenants/{id}/status checked existence at the routing layer and
        DELETE /v1/admin/tenants/{id} did not - the same failure mode guarded in one place and missed in the other,
        so the check moved into the store to fix it once for all (revoke_owner_tenant / delete_template take the
        same path).

        Constraint: the WHERE matches on id alone, independent of the previous status, so rowcount answers
             "does this tenant exist?" - setting the status to its current value also returns True."""
        if status not in {"active", "suspended"}:
            raise StoreError("status must be active or suspended")
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_tenants SET status = {ph}, "
                    "disabled_at = CASE WHEN {ph} = 'suspended' "
                    "THEN {now} ELSE NULL END WHERE id = {ph}"
                ),
                (status, status, tenant_id),
            )
            return (cursor.rowcount or 0) > 0

    # --- API key --------------------------------------------------------
    def issue_api_key(
        self,
        tenant_id: str | None,
        label: str,
        *,
        permissions: Iterable[str] = (),
        expires_in_seconds: int | None = None,
        now: int | None = None,
    ) -> tuple[str, ApiKey]:
        """Issue a key. Returns (plaintext, record) - the plaintext is never retrievable again."""
        if tenant_id == MANAGEMENT_TENANT:
            # A key scoped to the reserved row would be a tenant credential
            # over everything the management plane owns; the management plane
            # is an admin key (tenant_id None), never a tenant key.
            raise StoreError(f"tenant id is reserved: {tenant_id}")
        granted = frozenset(permissions)
        unknown = sorted(granted - KEY_PERMISSIONS)
        if unknown:
            raise StoreError(f"unknown key permission: {unknown[0]}")
        if expires_in_seconds is not None and expires_in_seconds < 1:
            raise StoreError("key lifetime must be at least one second")
        current = int(time.time() if now is None else now)
        expires_at = (
            None if expires_in_seconds is None else current + expires_in_seconds
        )
        plaintext = generate_key(tenant_id)
        key_id = secrets.token_hex(8)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_api_keys "
                    "(id, tenant_id, key_prefix, key_sha256, label, "
                    "permissions, expires_at) "
                    "VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
                ),
                (
                    key_id,
                    tenant_id,
                    plaintext[:KEY_PREFIX_LENGTH],
                    hash_key(plaintext),
                    label,
                    " ".join(sorted(granted)),
                    expires_at,
                ),
            )
        return plaintext, ApiKey(
            id=key_id,
            tenant_id=tenant_id,
            label=label,
            permissions=granted,
            expires_at=expires_at,
        )

    def authenticate(self, plaintext: str) -> ApiKey | None:
        """Look up by prefix, then compare the hash of the whole key in constant time.

        AI-LOCK: the comparison must go through hmac.compare_digest. With == the response time would vary with
             the length of the matching prefix, shrinking the brute-force search space from exponential to linear.
             The prefix is only for lookup; it is not a credential by itself."""
        if not plaintext:
            return None
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT id, tenant_id, key_sha256, label, permissions, "
                    "expires_at FROM sandbox_api_keys "
                    "WHERE key_prefix = {ph} AND revoked_at IS NULL"
                ),
                (plaintext[:KEY_PREFIX_LENGTH],),
            )
            row = cursor.fetchone()
        if not row:
            return None
        if not hmac.compare_digest(str(row[2]), hash_key(plaintext)):
            return None
        expires_at = None if row[5] is None else int(row[5])
        # Expiry is checked here rather than in the WHERE clause so that both
        # backends compare the same way, and so an expired key is one branch
        # away from an unknown one instead of being indistinguishable from it.
        if expires_at is not None and expires_at <= int(time.time()):
            return None
        return ApiKey(
            id=str(row[0]),
            tenant_id=row[1],
            label=str(row[3]),
            permissions=frozenset(str(row[4] or "").split()),
            expires_at=expires_at,
        )

    def touch_api_key(self, key_id: str) -> None:
        """Record one use, writing at most once per TOUCH_THROTTLE_SECONDS.

        The throttle condition lives in the WHERE rather than a SELECT-then-decide: one statement, no race, and
        under high-frequency calls the vast majority of requests change no row at all. Calls inside the window lose
        **no** information - last_used_at means "recently used", and minute-level accuracy is more than enough
        (see TOUCH_THROTTLE_SECONDS).
        Constraint: last_used_at is therefore not "the exact moment of the last request" but an approximation
             accurate to one window. Anything that displays it should say so."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_api_keys SET last_used_at = {now} "
                    "WHERE id = {ph} "
                    "AND (last_used_at IS NULL OR last_used_at < {stale})"
                ),
                (key_id,),
            )

    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke a key. Returns whether the key id exists (an already-revoked key still counts as existing).

        rowcount must be checked: otherwise revoking a key that does not exist at all returns a green light, and an
        operator who mistypes one character believes the permission is gone while the real key is still live. This
        is worth guarding even more than "suspend a non-existent tenant", because the failure direction is
        "believed to be tightened, actually not".

        Why it returns "exists" rather than "a row really changed this time": rowcount 0 has two causes - the id
        does not exist, or it was already revoked. Revocation must be idempotent (a retry after a timeout must not
        see 404), so the two have to be told apart, hence one extra lookup on a miss. Both statements run in the
        same _cursor() block, i.e. the same transaction.

        Constraint: the UPDATE keeps `AND revoked_at IS NULL`. Dropping it would still answer existence in one
             statement, but would rewrite revoked_at to now - "when was this key revoked" is needed for later
             tracing and must not be erased by a retry."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_api_keys SET revoked_at = {now} "
                    "WHERE id = {ph} AND revoked_at IS NULL"
                ),
                (key_id,),
            )
            if (cursor.rowcount or 0) > 0:
                return True
            cursor.execute(
                self._sql(
                    "SELECT 1 FROM sandbox_api_keys WHERE id = {ph}"
                ),
                (key_id,),
            )
            return cursor.fetchone() is not None

    def list_api_keys(self, tenant_id: str | None) -> list[dict[str, Any]]:
        select = (
            "SELECT id, tenant_id, key_prefix, label, created_at, "
            "last_used_at, revoked_at, permissions, expires_at "
            "FROM sandbox_api_keys "
        )
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    select
                    + "WHERE tenant_id IS NOT DISTINCT FROM {ph} ORDER BY created_at"
                    if self._dialect.name == "postgresql"
                    else select + "WHERE tenant_id <=> {ph} ORDER BY created_at"
                    if self._dialect.name == "mysql"
                    else select + "WHERE tenant_id IS {ph} ORDER BY created_at"
                ),
                (tenant_id,),
            )
            rows = cursor.fetchall()
        return [
            {
                "id": row[0],
                "tenant_id": row[1],
                "key_prefix": row[2],
                "label": row[3],
                "created_at": _json_timestamp(row[4]),
                "last_used_at": _json_timestamp(row[5]),
                "revoked_at": _json_timestamp(row[6]),
                "permissions": sorted(str(row[7] or "").split()),
                "expires_at": None if row[8] is None else int(row[8]),
            }
            for row in rows
        ]

    # --- Workspace ownership --------------------------------------------------
    def register_workspace(
        self,
        tenant_id: str,
        workspace_id: str,
        *,
        principal_kind: str,
        principal_id: str,
        session_key: str,
    ) -> None:
        """Register ownership only; no quota is evaluated.

        Kept because "registering ownership" and "taking a quota slot" are two different things: the management
        plane back-filling an ownership record on a tenant's behalf, or a test fixture seeding its initial state,
        should not be blocked by tenant quota. The path where tenants create for themselves **must** go through
        admit_workspace - that is the quota gate."""
        self.admit_workspace(
            tenant_id,
            workspace_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            session_key=session_key,
            limit=None,
        )

    def admit_workspace(
        self,
        tenant_id: str,
        workspace_id: str,
        *,
        principal_kind: str,
        principal_id: str,
        session_key: str,
        limit: int | None,
    ) -> str:
        """Atomically take a Workspace quota slot and register ownership. Returns one of three outcomes.

        WORKSPACE_ADMITTED     a new slot was taken this time and a row written.
        WORKSPACE_REUSED       the same Workspace of the same tenant is already recorded (idempotent re-entry);
                               only last_used_at is refreshed and no new slot is taken.
        WORKSPACE_AT_CAPACITY  the conditional insert affected 0 rows - a concurrent request took the last slot.
                               **Neither success nor error**; the caller should treat it as 429.
        limit=None skips the quota check entirely (see register_workspace).

        Responsibility: only "is there a slot" and "write this row"; never touches the volume, never creates a directory.
        Constraint: counting once and taking one must be in the same serialization, otherwise two requests with
             different session_ids both count limit-1 and both insert - a single replica can over-admit by one,
             N concurrent requests by N-1. PostgreSQL first locks the tenant row with SELECT ... FOR UPDATE so
             admissions for the same tenant queue; SQLite has no such syntax and needs none: the conditional
             insert is one statement and its count subquery is evaluated under the write lock.
        Boundary: soft-deleted rows (deleted_at not NULL) do not count toward quota, so the WHERE of the counting
             subquery must be verbatim identical to count_workspaces. If either side changes alone the quota
             starts to drift - silently, since both places return a number and neither raises.
        Boundary: idempotent re-entry takes no new slot. Being unable to use an existing Workspace once the quota
             is full (reconnect ⇒ 429) would be bizarre, so the "already recorded" branch must come before the
             quota check.

        🔴 AI-LOCK: correctness relies on PostgreSQL READ COMMITTED (the default isolation level this repository
           configures). The conditional insert is the statement after FOR UPDATE in the same transaction and takes
           a **fresh statement-level snapshot**, so it sees the row T1 committed when it released the lock. Under
           REPEATABLE READ the whole transaction shares the snapshot taken at its start: T2 acquires the tenant row
           lock but still cannot see T1's insert ⇒ the condition always holds ⇒ silent over-admission. No error,
           no log, only quota overrun in production. Raising the isolation level means also changing this to
           SERIALIZABLE plus serialization-failure retry.
           SQLite holds by "only one write transaction at a time": the counting subquery and the insert are one
           statement and isolation-level configuration does not affect them. The same dependency applies to
           admit_runtime."""
        for value, name in (
            (principal_kind, "principal.kind"),
            (principal_id, "principal.id"),
            (session_key, "session_id"),
        ):
            if not FREEFORM.fullmatch(value):
                raise StoreError(f"{name} must be 1-128 chars without control bytes")
        with self._cursor() as cursor:
            if limit is not None:
                # Lock the tenant row so concurrent admissions for the same tenant queue up. Without a quota there is
                # nothing to serialize, and a pure registration should not pay for a row lock.
                cursor.execute(
                    self._sql(
                        "SELECT id FROM sandbox_tenants "
                        "WHERE id = {ph} {row_lock}"
                    ),
                    (tenant_id,),
                )
                if cursor.fetchone() is None:
                    raise StoreError(f"unknown tenant: {tenant_id}")
            cursor.execute(
                self._sql(
                    "SELECT tenant_id FROM sandbox_workspaces "
                    "WHERE workspace_id = {ph} AND deleted_at IS NULL"
                ),
                (workspace_id,),
            )
            row = cursor.fetchone()
            if row and str(row[0]) != tenant_id:
                # The derivation function guarantees no collisions. A hit here means the SIGNING_KEY was rotated or
                # the input was forged; either way, someone else's Workspace must not be silently re-recorded under
                # this tenant's name.
                raise StoreError("workspace already belongs to another tenant")
            if row:
                cursor.execute(
                    self._sql(
                        "UPDATE sandbox_workspaces SET last_used_at = {now} "
                        "WHERE tenant_id = {ph} AND workspace_id = {ph}"
                    ),
                    (tenant_id, workspace_id),
                )
                return WORKSPACE_REUSED
            # A reclaimed Workspace comes back under the same derived id while the
            # soft-deleted row is retained for audit, so re-registration revives that
            # row instead of inserting into the same primary key. The revival passes
            # through the same quota gate as a fresh admission. The live count is read
            # through a derived table because MySQL rejects a subquery on the table
            # being updated.
            cursor.execute(
                self._sql(
                    "SELECT 1 FROM sandbox_workspaces "
                    "WHERE tenant_id = {ph} AND workspace_id = {ph} "
                    "AND deleted_at IS NOT NULL"
                ),
                (tenant_id, workspace_id),
            )
            if cursor.fetchone() is not None:
                revive = (
                    "UPDATE sandbox_workspaces SET deleted_at = NULL, "
                    "principal_kind = {ph}, principal_id = {ph}, session_key = {ph}, "
                    "created_at = {now}, last_used_at = {now} "
                    "WHERE tenant_id = {ph} AND workspace_id = {ph} "
                    "AND deleted_at IS NOT NULL"
                )
                params = (principal_kind, principal_id, session_key, tenant_id, workspace_id)
                if limit is not None:
                    revive += (
                        " AND (SELECT COUNT(*) FROM (SELECT 1 FROM sandbox_workspaces"
                        "  WHERE tenant_id = {ph} AND deleted_at IS NULL) AS live) < {ph}"
                    )
                    params += (tenant_id, limit)
                cursor.execute(self._sql(revive), params)
                return (
                    WORKSPACE_ADMITTED if cursor.rowcount == 1 else WORKSPACE_AT_CAPACITY
                )
            columns = (
                "INSERT INTO sandbox_workspaces "
                "(tenant_id, workspace_id, principal_kind, principal_id, "
                "session_key) "
            )
            values = (
                tenant_id,
                workspace_id,
                principal_kind,
                principal_id,
                session_key,
            )
            if limit is None:
                cursor.execute(
                    self._sql(columns + "VALUES ({ph}, {ph}, {ph}, {ph}, {ph})"),
                    values,
                )
                return WORKSPACE_ADMITTED
            cursor.execute(
                self._sql(
                    columns + "SELECT {ph}, {ph}, {ph}, {ph}, {ph} WHERE ("
                    "  SELECT COUNT(*) FROM sandbox_workspaces"
                    "  WHERE tenant_id = {ph} AND deleted_at IS NULL"
                    ") < {ph}"
                ),
                (*values, tenant_id, limit),
            )
            # 🔴 Zero rows is neither a success nor an exception: the condition failed, meaning a concurrent request
            # just took the last slot. Without an explicit branch this would pass as "written successfully" - this
            # repository has already been bitten by "UPDATE affected 0 rows is not an error, so the carefully
            # written error log never fired".
            return (
                WORKSPACE_ADMITTED
                if cursor.rowcount == 1
                else WORKSPACE_AT_CAPACITY
            )

    def owner_of(self, workspace_id: str) -> str | None:
        """Which tenant owns this Workspace? Asked on the authentication path."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id FROM sandbox_workspaces "
                    "WHERE workspace_id = {ph} AND deleted_at IS NULL"
                ),
                (workspace_id,),
            )
            row = cursor.fetchone()
        return str(row[0]) if row else None

    def workspace_tenant_active(self, workspace_id: str) -> bool | None:
        """Whether the tenant behind this Workspace is still active. None = the store has no ownership row for it.

        The only difference from "owner_of, then get_tenant" is the connection count: every `_cursor()` opens a
        new connection and holds the Store lock throughout, and this query runs on every scoped **data-plane**
        request - folding it into one JOIN brings connections per request back from 2 to 1.

        Constraint: None and False are different things and the caller must not merge them into one branch. A
             Workspace with no ownership row (created by the management-plane identity, see the
             `tenant_id is not None` condition in POST /v1/workspaces) belongs to no tenant at all; that is
             "this gate does not apply", not "the tenant is suspended"."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT t.status FROM sandbox_workspaces w "
                    "JOIN sandbox_tenants t ON t.id = w.tenant_id "
                    "WHERE w.workspace_id = {ph} AND w.deleted_at IS NULL"
                ),
                (workspace_id,),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0]) == "active"

    def list_workspaces(self, tenant_id: str | None) -> list[dict[str, Any]]:
        """tenant_id None means the management plane's full view."""
        clause = "" if tenant_id is None else "AND tenant_id = {ph}"
        params: tuple[Any, ...] = () if tenant_id is None else (tenant_id,)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id, workspace_id, principal_kind, principal_id, "
                    f"session_key FROM sandbox_workspaces WHERE deleted_at IS NULL {clause} "
                    "ORDER BY tenant_id, workspace_id"
                ),
                params,
            )
            rows = cursor.fetchall()
        return [
            {
                "tenant_id": row[0],
                "workspace_id": row[1],
                "principal_kind": row[2],
                "principal_id": row[3],
                "session_key": row[4],
            }
            for row in rows
        ]

    def count_workspaces(self, tenant_id: str) -> int:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT COUNT(*) FROM sandbox_workspaces "
                    "WHERE tenant_id = {ph} AND deleted_at IS NULL"
                ),
                (tenant_id,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def forget_workspace(self, tenant_id: str, workspace_id: str) -> None:
        """Soft delete. The row is kept so that an audit can still answer "who did this id belong to"."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_workspaces SET deleted_at = {now} "
                    "WHERE tenant_id = {ph} AND workspace_id = {ph} "
                    "AND deleted_at IS NULL"
                ),
                (tenant_id, workspace_id),
            )

    # --- Audit ---------------------------------------------------------------
    def record_audit(
        self,
        *,
        actor_kind: str,
        actor_id: str | None,
        action: str,
        outcome: str,
        tenant_id: str | None = None,
        target: str | None = None,
    ) -> None:
        """Write one audit entry.

        🔴 The caller must treat StoreError as "audit not written", not "business request failed": rejecting the
           business request when the audit write fails would let the availability of the audit table decide the
           availability of the service. It must not be swallowed silently either - then audit could fail with
           nobody noticing. The right handling is a metric plus a log line so that "auditing is failing" is observable."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_audit_log "
                    "(id, tenant_id, actor_kind, actor_id, action, target, "
                    "outcome) VALUES "
                    "({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
                ),
                (
                    secrets.token_hex(16),
                    tenant_id,
                    actor_kind,
                    actor_id,
                    action,
                    target,
                    outcome,
                ),
            )

    def list_audit(
        self, *, tenant_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Audit entries newest first. tenant_id None means the global (management-plane) view."""
        capped = max(1, min(int(limit), 1000))
        clause = "WHERE tenant_id = {ph} " if tenant_id else ""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT id, tenant_id, actor_kind, actor_id, action, "
                    "target, outcome, created_at FROM sandbox_audit_log "
                    + clause
                    + "ORDER BY created_at DESC, id DESC LIMIT {ph}"
                ),
                (*params, capped),
            )
            rows = cursor.fetchall()
        return [
            {
                "id": row[0],
                "tenant": row[1],
                "actor_kind": row[2],
                "actor_id": row[3],
                "action": row[4],
                "target": row[5],
                "outcome": row[6],
                "created_at": _json_timestamp(row[7]),
            }
            for row in rows
        ]

    # --- Runtime life cycle ------------------------------------------------
    #: The statuses that hold a quota slot. released/failed are terminal; their rows stay for audit and failure-rate statistics.
    LIVE_RUNTIME_STATUSES = ("pending", "active")

    def admit_runtime(
        self,
        tenant_id: str,
        sandbox_id: str,
        workspace_id: str,
        template_id: str,
        limit: int,
    ) -> bool:
        """Atomically take a Runtime slot. Returns True if a slot was taken, False if the quota is full.

        Responsibility: only "is there a slot left" and "write this row"; never touches Kubernetes.
        Constraint: counting once and taking one must be in the same serialization, otherwise two concurrent
             requests both count limit-1 and both insert. PostgreSQL first locks the tenant row with
             SELECT ... FOR UPDATE; SQLite has no such syntax and needs none - its write transaction is already
             database-level serial. One `_cursor()` block is one transaction, so the two statements commit or
             roll back together.

        🔴 This is the gate that replaced the in-process lock. With more than one Control Plane replica an in-process lock
           is no lock at all, and "quota exceeded" then shows up as node OOM rather than a clear rejection log."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT id FROM sandbox_tenants "
                    "WHERE id = {ph} {row_lock}"
                ),
                (tenant_id,),
            )
            if cursor.fetchone() is None:
                raise StoreError(f"unknown tenant: {tenant_id}")
            placeholders = ", ".join(
                "{ph}" for _ in self.LIVE_RUNTIME_STATUSES
            )
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_runtimes "
                    "(tenant_id, sandbox_id, workspace_id, template_id, status)"
                    " SELECT {ph}, {ph}, {ph}, {ph}, 'pending' WHERE ("
                    "  SELECT COUNT(*) FROM sandbox_runtimes"
                    f"  WHERE tenant_id = {{ph}} AND status IN ({placeholders})"
                    ") < {ph}"
                ),
                (
                    tenant_id,
                    sandbox_id,
                    workspace_id,
                    template_id,
                    tenant_id,
                    *self.LIVE_RUNTIME_STATUSES,
                    limit,
                ),
            )
            return cursor.rowcount == 1

    def record_untenanted_runtime(
        self, sandbox_id: str, workspace_id: str, template_id: str
    ) -> None:
        """Write a pending record that belongs to no tenant and takes no quota slot.

        Why it exists: **writing a status row** and **taking a tenant's quota slot** are two different things, and
        admit_runtime's INSERT ties them together - so identities without a tenant (the global management-plane
        key, the static SANDBOX_CONTROL_PLANE_TOKEN) used to leave no row at all for the runtime they provisioned.
        `GET /v1/sandboxes/{id}` could only answer `unknown`, and the contract "take the 202, then poll for the
        terminal state" did not hold under those identities.

        🔴 tenant_id is hard-coded to UNTENANTED_RUNTIME by this method and is **not a parameter**. Making it one
           would create an unconditional insert path that bypasses admit_runtime: pass a real tenant id and the
           per-tenant quota gate is gone.

        Constraint: insert only, no state transitions - activate/release still go exclusively through the
             conditional update in _transition, so this method is not an "unconditional write" bypass."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_runtimes "
                    "(tenant_id, sandbox_id, workspace_id, template_id, status)"
                    " VALUES ({ph}, {ph}, {ph}, {ph}, 'pending')"
                ),
                (UNTENANTED_RUNTIME, sandbox_id, workspace_id, template_id),
            )

    def activate_runtime(self, tenant_id: str, sandbox_id: str) -> bool:
        """pending → active. Returns False = the row is no longer pending.

        🔴 False is not an error: a concurrent release may have moved it to a terminal state (the sandbox was
           deleted halfway through provisioning). The caller should treat it as "this creation has been
           invalidated", not retry."""
        return self._transition(
            tenant_id, sandbox_id, "active", ("pending",)
        )

    def release_runtime(
        self, tenant_id: str, sandbox_id: str, *, failed: bool = False
    ) -> bool:
        """→ released/failed. Returns False = already terminal (repeated release).

        A repeated release returns False rather than raising: DELETE must be idempotent, and a retrying caller
        should not see an error."""
        return self._transition(
            tenant_id,
            sandbox_id,
            "failed" if failed else "released",
            self.LIVE_RUNTIME_STATUSES,
        )

    def _transition(
        self,
        tenant_id: str,
        sandbox_id: str,
        target: str,
        expected: tuple[str, ...],
    ) -> bool:
        """Conditional update. The only entry point for every status write to this table.

        Never add an unconditional-write bypass: it could overwrite a concurrent
        terminal state and make reconciliation delete valid data."""
        placeholders = ", ".join("{ph}" for _ in expected)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_runtimes SET status = {ph}, "
                    "updated_at = {now} "
                    "WHERE tenant_id = {ph} AND sandbox_id = {ph} "
                    f"AND status IN ({placeholders})"
                ),
                (target, tenant_id, sandbox_id, *expected),
            )
            return cursor.rowcount == 1

    def release_stale_pending_runtimes(self, older_than_seconds: int) -> int:
        """Mark rows stuck in pending for too long as failed; returns how many.

        pending means "quota slot taken, Pod not ready yet". Normally it turns into active or is rolled back
        within seconds. It only gets stuck two ways: the Control Plane was killed mid-provisioning (rollback never ran),
        or the rollback itself failed. Left alone, such rows eat the tenant's quota forever, and all that shows
        in production is "the quota is inexplicably full".

        🔴 The threshold must be well above the provisioning ceiling (90s waiting for the Pod to be ready, plus the
           health probe), otherwise a sandbox that is still being provisioned gets misjudged - far worse than a
           leaked slot."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_runtimes SET status = 'failed', "
                    "updated_at = {now} "
                    "WHERE status = 'pending' AND updated_at < {age_cutoff}"
                ),
                (older_than_seconds,),
            )
            return max(cursor.rowcount, 0)

    def idle_workspaces(self, older_than_seconds: int) -> list[str]:
        """Workspace ids the control plane has not seen used for this long.

        Responsibility: answer "is this Workspace idle" from the control plane's
        own record; not "does the directory still exist on the volume".

        🔴 The idle verdict must never be read off the volume. `.sandbox/last_used_at`
           lives inside the tenant's own writable tree and runs as the same uid as the
           shell, so a single `rm -rf /workspace/.sandbox` used to make a Workspace
           unreclaimable - and since the global cap counts directories on the volume,
           one tenant could fill it and turn every other tenant's create into a 429.
           `admit_workspace` already refreshes this column on every admission."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT workspace_id FROM sandbox_workspaces "
                    "WHERE deleted_at IS NULL AND last_used_at < {age_cutoff}"
                ),
                (older_than_seconds,),
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def get_runtime(self, sandbox_id: str) -> dict[str, Any] | None:
        """Fetch a Runtime record by id, terminal states included.

        Difference from runtime_owner: that one only sees live rows and only returns the tenant, for
        authentication; this one must return the status, which an asynchronous creator relies on to tell
        whether provisioning has finished."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id, sandbox_id, workspace_id, template_id, "
                    "status, created_at, updated_at FROM sandbox_runtimes "
                    "WHERE sandbox_id = {ph}"
                ),
                (sandbox_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "tenant": row[0],
            "sandbox_id": row[1],
            "workspace_id": row[2],
            "template": row[3],
            "status": row[4],
            "created_at": _json_timestamp(row[5]),
            "updated_at": _json_timestamp(row[6]),
        }

    def runtime_epoch(self, sandbox_id: str) -> int | None:
        """The capability epoch of a live sandbox, or None when there is none.

        Terminal rows deliberately do not answer: a released sandbox must not be
        able to hand out a working ticket, and the id may not be reused."""
        placeholders = ", ".join("{ph}" for _ in self.LIVE_RUNTIME_STATUSES)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT token_epoch FROM sandbox_runtimes "
                    f"WHERE sandbox_id = {{ph}} AND status IN ({placeholders})"
                ),
                (sandbox_id, *self.LIVE_RUNTIME_STATUSES),
            )
            row = cursor.fetchone()
        return None if row is None else int(row[0])

    def workspace_epoch(self, workspace_id: str) -> int | None:
        """The capability epoch of a Workspace that has not been deleted."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT token_epoch FROM sandbox_workspaces "
                    "WHERE workspace_id = {ph} AND deleted_at IS NULL"
                ),
                (workspace_id,),
            )
            row = cursor.fetchone()
        return None if row is None else int(row[0])

    def bump_runtime_epoch(self, sandbox_id: str) -> int | None:
        """Revoke every ticket outstanding for this sandbox. Returns the new epoch.

        🔴 The revocation is what stops Control Plane minting anything the old
        instance key accepts, and what makes a re-provisioned sandbox derive a
        different key. Tickets already handed out stay valid until their own
        expiry - that window is the ticket TTL and nothing longer."""
        return self._bump_epoch(
            "sandbox_runtimes", "sandbox_id", sandbox_id, "status <> 'released'"
        )

    def bump_workspace_epoch(self, workspace_id: str) -> int | None:
        """Rotate the Workspace capability epoch (a new Runtime, or a revocation)."""
        return self._bump_epoch(
            "sandbox_workspaces", "workspace_id", workspace_id, "deleted_at IS NULL"
        )

    def _bump_epoch(
        self, table: str, column: str, value: str, condition: str
    ) -> int | None:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    f"UPDATE {table} SET token_epoch = token_epoch + 1 "
                    f"WHERE {column} = {{ph}} AND {condition}"
                ),
                (value,),
            )
            cursor.execute(
                self._sql(
                    f"SELECT token_epoch FROM {table} "
                    f"WHERE {column} = {{ph}} AND {condition}"
                ),
                (value,),
            )
            row = cursor.fetchone()
        return None if row is None else int(row[0])

    def count_all_live_runtimes(self) -> int:
        """Number of live runtimes across all tenants. For the /metrics gauge."""
        placeholders = ", ".join("{ph}" for _ in self.LIVE_RUNTIME_STATUSES)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT COUNT(*) FROM sandbox_runtimes "
                    f"WHERE status IN ({placeholders})"
                ),
                self.LIVE_RUNTIME_STATUSES,
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def count_all_workspaces(self) -> int:
        """Number of non-deleted Workspaces across all tenants. For the /metrics gauge."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT COUNT(*) FROM sandbox_workspaces "
                    "WHERE deleted_at IS NULL"
                )
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def runtime_owner(self, sandbox_id: str) -> str | None:
        """Who owns this sandbox? Terminal rows do not count - a released id is as good as nonexistent."""
        placeholders = ", ".join("{ph}" for _ in self.LIVE_RUNTIME_STATUSES)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id FROM sandbox_runtimes "
                    f"WHERE sandbox_id = {{ph}} AND status IN ({placeholders})"
                ),
                (sandbox_id, *self.LIVE_RUNTIME_STATUSES),
            )
            row = cursor.fetchone()
        return str(row[0]) if row else None

    def runtime_tenant_active(self, sandbox_id: str) -> bool | None:
        """Whether the tenant behind this sandbox is still active. None = the store has no row for it.

        Deliberately **not** filtered by LIVE_RUNTIME_STATUSES: ownership and liveness are two different
        questions, and only ownership is asked here. Borrowing runtime_owner's "live rows only" semantics would
        drop a just-released sandbox into the None branch (= allow), which is exactly the kind of request that
        must be blocked after a tenant is suspended."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT t.status FROM sandbox_runtimes r "
                    "JOIN sandbox_tenants t ON t.id = r.tenant_id "
                    "WHERE r.sandbox_id = {ph}"
                ),
                (sandbox_id,),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0]) == "active"

    def list_live_runtimes(self) -> list[dict[str, Any]]:
        """All non-terminal runtimes. Used for reconciliation (the store has it, the cluster does not)."""
        placeholders = ", ".join("{ph}" for _ in self.LIVE_RUNTIME_STATUSES)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id, sandbox_id, workspace_id, status, "
                    "created_at FROM sandbox_runtimes "
                    f"WHERE status IN ({placeholders}) ORDER BY created_at"
                ),
                self.LIVE_RUNTIME_STATUSES,
            )
            rows = cursor.fetchall()
        return [
            {
                "tenant": row[0],
                "sandbox_id": row[1],
                "workspace_id": row[2],
                "status": row[3],
                "created_at": _json_timestamp(row[4]),
            }
            for row in rows
        ]

    # --- Template ------------------------------------------------------------------
    def put_template(
        self,
        tenant_id: str,
        template_id: str,
        image: str,
        *,
        created_by: str | None = None,
    ) -> None:
        """Register a template. Writing the same (tenant_id, template_id) again overwrites.

        Why overwrite rather than conflict: the only caller of this path is admin, and a retry after a timeout must
        be safe - answering 409 would turn "did my write succeed?" into a manual check. Changing the image is
        likewise a legitimate admin action, and it still leaves a trace (created_by).

        On overwrite created_at is refreshed together with created_by: the two columns describe the same write,
        and refreshing only one would make the audit read "A placed this image three months ago" when B replaced it
        yesterday. Soft-deleted rows are revived here too, without having to be deleted first."""
        if tenant_id != GLOBAL_TENANT and not TENANT_ID.fullmatch(tenant_id):
            raise StoreError(
                f"template tenant must be {GLOBAL_TENANT!r} or a valid tenant id"
            )
        if not TEMPLATE_ID.fullmatch(template_id):
            raise StoreError("template id must match ^[a-z0-9][-a-z0-9]{0,31}$")
        if not IMAGE_REF.fullmatch(image):
            raise StoreError("image must be 1-256 chars without control bytes")
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT 1 FROM sandbox_templates "
                    "WHERE tenant_id = {ph} AND template_id = {ph}"
                ),
                (tenant_id, template_id),
            )
            if cursor.fetchone():
                cursor.execute(
                    self._sql(
                        "UPDATE sandbox_templates SET image = {ph}, "
                        "created_by = {ph}, created_at = {now}, deleted_at = NULL "
                        "WHERE tenant_id = {ph} AND template_id = {ph}"
                    ),
                    (image, created_by, tenant_id, template_id),
                )
                return
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_templates "
                    "(tenant_id, template_id, image, created_by) "
                    "VALUES ({ph}, {ph}, {ph}, {ph})"
                ),
                (tenant_id, template_id, image, created_by),
            )

    def visible_templates(self, tenant_id: str | None) -> list[dict[str, Any]]:
        """Templates visible to one tenant: global ones plus its own.

        tenant_id None means "acting for no tenant" (a management-plane request without X-Sandbox-Tenant), and
        then only global templates are returned - deliberately **different** from the "everything" semantics of
        list_workspaces(None): templates end up in Pod specs, and if other tenants' templates leaked into this list
        a single typo could run tenant A's image in tenant B's sandbox. The management plane's full view is
        list_templates."""
        if tenant_id is None or tenant_id == GLOBAL_TENANT:
            clause = "tenant_id = {ph}"
            params: tuple[Any, ...] = (GLOBAL_TENANT,)
        else:
            clause = "tenant_id IN ({ph}, {ph})"
            params = (GLOBAL_TENANT, tenant_id)
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id, template_id, image, created_at, created_by "
                    f"FROM sandbox_templates WHERE deleted_at IS NULL AND {clause} "
                    "ORDER BY tenant_id, template_id"
                ),
                params,
            )
            rows = cursor.fetchall()
        return [_template_from_row(row) for row in rows]

    def list_templates(self) -> list[dict[str, Any]]:
        """The management plane's full view. Soft-deleted rows are excluded - listing them after deletion would make "delete" meaningless."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT tenant_id, template_id, image, created_at, created_by "
                    "FROM sandbox_templates WHERE deleted_at IS NULL "
                    "ORDER BY tenant_id, template_id"
                )
            )
            rows = cursor.fetchall()
        return [_template_from_row(row) for row in rows]

    def delete_template(self, tenant_id: str, template_id: str) -> bool:
        """Soft delete. Returns whether a row was actually deleted this time, so the caller can tell "deleted" from "never existed"."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "UPDATE sandbox_templates SET deleted_at = {now} "
                    "WHERE tenant_id = {ph} AND template_id = {ph} "
                    "AND deleted_at IS NULL"
                ),
                (tenant_id, template_id),
            )
            return (cursor.rowcount or 0) > 0

    def allow_owner_tenant(
        self,
        tenant_id: str,
        owner_tenant: str,
        *,
        created_by: str | None = None,
    ) -> None:
        """Register "this accessing party may act for this owner tenant segment".

        Repeated registration is idempotent: the only caller is admin, and a retry after a timeout must be safe -
        answering 409 would turn "did my write succeed?" into manual troubleshooting (same reasoning as
        put_template)."""
        if not TENANT_ID.fullmatch(tenant_id):
            raise StoreError(f"invalid tenant id: {tenant_id!r}")
        if not OWNER_TENANT_SEGMENT.fullmatch(owner_tenant):
            raise StoreError(f"invalid owner tenant segment: {owner_tenant!r}")
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "DELETE FROM sandbox_owner_prefixes "
                    "WHERE tenant_id = {ph} AND owner_tenant = {ph}"
                ),
                (tenant_id, owner_tenant),
            )
            cursor.execute(
                self._sql(
                    "INSERT INTO sandbox_owner_prefixes "
                    "(tenant_id, owner_tenant, created_by) "
                    "VALUES ({ph}, {ph}, {ph})"
                ),
                (tenant_id, owner_tenant, created_by),
            )

    def revoke_owner_tenant(self, tenant_id: str, owner_tenant: str) -> bool:
        """Remove a registration. Returns whether a row was actually deleted this time.

        rowcount must be checked: otherwise removing a registration that never existed returns "success", and an
        operator who mistypes one character believes the permission is gone while the real one is still live."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "DELETE FROM sandbox_owner_prefixes "
                    "WHERE tenant_id = {ph} AND owner_tenant = {ph}"
                ),
                (tenant_id, owner_tenant),
            )
            return (cursor.rowcount or 0) > 0

    def owner_tenant_active(self, owner_tenant: str) -> bool | None:
        """Is any tenant this owner tenant segment resolves to still active?

        Two ways it resolves, and both must be tried. A segment the Control Plane derived from a credential **is** a
        sandbox_tenants.id and carries no registration row, so a registration-only lookup would answer "nobody
        knows" for every object a tenant credential ever wrote - which is the answer that lets a suspended
        tenant's outstanding tickets keep working. A segment a management-plane caller declared belongs to no
        tenant by construction, and sandbox_owner_prefixes is the only thing that attributes it.

        None = neither answered. **Not** False: a declared segment nobody registered means the deployment has
        not configured that half of the gate, not that the segment belongs to a suspended tenant.

        With several registrants, "any one active counts as active": the primary key is (tenant_id, owner_tenant),
        the same owner segment can be registered by several accessing parties, and suspending one of them must not
        take the others down with it."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT t.status FROM sandbox_owner_prefixes p "
                    "JOIN sandbox_tenants t ON t.id = p.tenant_id "
                    "WHERE p.owner_tenant = {ph} "
                    "UNION ALL "
                    "SELECT status FROM sandbox_tenants WHERE id = {ph}"
                ),
                (owner_tenant, owner_tenant),
            )
            rows = cursor.fetchall()
        if not rows:
            return None
        return any(str(row[0]) == "active" for row in rows)

    def list_owner_prefixes(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT owner_tenant, created_at, created_by "
                    "FROM sandbox_owner_prefixes WHERE tenant_id = {ph} "
                    "ORDER BY owner_tenant"
                ),
                (tenant_id,),
            )
            rows = cursor.fetchall()
        return [
            {
                "owner_tenant": str(row[0]),
                "created_at": _json_timestamp(row[1]),
                "created_by": row[2],
            }
            for row in rows
        ]


def _json_timestamp(value: Any) -> Any:
    """Timestamps always leave as strings, so the backends really do behave the same from outside.

    🔴 This is the one **inherent** inconsistency between the backends: SQLite stores timestamps as TEXT and
    returns strings; psycopg parses TIMESTAMPTZ into datetime. The Control Plane's send_json is a bare json.dumps - so
    the same code was green all the way on SQLite and hit TypeError → 500 the first time it met PostgreSQL, and
    only on the endpoints that carry timestamps. It cannot be caught locally.

    Normalization lives here rather than as default=str on send_json: the latter would also quietly stringify
    any object that has no business being in a response, hiding "returned something it should not have" bugs."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _template_from_row(row: Any) -> dict[str, Any]:
    return {
        "tenant_id": row[0],
        "template_id": row[1],
        "image": row[2],
        "created_at": _json_timestamp(row[3]),
        "created_by": row[4],
    }


def _tenant_from_row(row: Any) -> Tenant | None:
    if not row:
        return None
    return Tenant(
        id=str(row[0]),
        display_name=str(row[1]),
        status=str(row[2]),
        max_workspaces=int(row[3]),
        max_runtimes=int(row[4]),
    )
