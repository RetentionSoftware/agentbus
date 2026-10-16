"""Database schema and its migrations.

The schema version lives in `PRAGMA user_version`. Version 0 means "not stamped yet": either a new, empty file or a
database created before versioning existed; both get the baseline tables (`CREATE TABLE IF NOT EXISTS`), are checked
against the expected columns and stamped with version 1. Later changes are numbered steps in `MIGRATIONS`; each runs
in its own transaction, and an online backup of the database is taken before the first one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 1

BASELINE = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'idle',
    summary TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    registered_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,          -- agent name or '*' for broadcast
    kind TEXT NOT NULL,
    priority TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    reply_to INTEGER,
    task_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, id);
CREATE TABLE IF NOT EXISTS reads (
    message_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    read_at REAL NOT NULL,
    via TEXT NOT NULL,                -- 'inbox', 'hook' or 'preexisting'
    PRIMARY KEY (message_id, agent)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    spec_path TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'todo',
    owner TEXT NOT NULL DEFAULT '',
    depends_on TEXT NOT NULL DEFAULT '[]',
    wave TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    path TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    shared INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    released_at REAL
);
"""

# Columns of the baseline tables, used to recognise a pre-versioning agentbus database.
BASELINE_COLUMNS: dict[str, set[str]] = {
    "events": {"id", "ts", "type", "agent", "target", "text"},
    "agents": {
        "name",
        "role",
        "session_key",
        "cwd",
        "branch",
        "state",
        "summary",
        "task_id",
        "registered_at",
        "last_seen",
    },
    "messages": {"id", "ts", "sender", "recipient", "kind", "priority", "subject", "body", "reply_to", "task_id"},
    "reads": {"message_id", "agent", "read_at", "via"},
    "tasks": {"id", "title", "spec_path", "state", "owner", "depends_on", "wave", "created_at", "updated_at"},
    "task_log": {"id", "task_id", "ts", "agent", "state", "note"},
    "claims": {"id", "agent", "path", "reason", "shared", "created_at", "expires_at", "released_at"},
}

Migration = Callable[[sqlite3.Connection], None]

# MIGRATIONS[n] upgrades a database from version n to n + 1 (n >= 1). It runs inside a transaction and must not
# commit; the version stamp is written by `migrate()`.
MIGRATIONS: dict[int, Migration] = {}


class SchemaError(Exception):
    """The database cannot be used by this version of agentbus; the message says why."""


def schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}


def _check_baseline(db: sqlite3.Connection) -> None:
    for table, expected in BASELINE_COLUMNS.items():
        actual = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if actual != expected:
            raise SchemaError(
                f"table '{table}' has columns {sorted(actual)}, expected {sorted(expected)}; "
                "this is not an agentbus database"
            )


def backup(db: sqlite3.Connection, db_path: Path, version: int) -> Path:
    """Copies the whole database next to it as `<name>.v<version>.bak` (consistent even with WAL readers)."""
    target = db_path.with_name(f"{db_path.name}.v{version}.bak")
    with sqlite3.connect(target) as copy:
        db.backup(copy)
    copy.close()
    return target


def migrate(db: sqlite3.Connection, db_path: Path) -> list[str]:
    """Brings the database to SCHEMA_VERSION. Returns human-readable notes about what was done.

    `db` must be in autocommit mode (isolation_level=None): transactions are managed here explicitly.
    """
    notes: list[str] = []
    version = schema_version(db)
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"database {db_path} has schema version {version}, but this agentbus supports up to "
            f"{SCHEMA_VERSION}; upgrade agentbus (downgrades are not supported)"
        )
    if version == 0:
        foreign = _tables(db) - BASELINE_COLUMNS.keys()
        if foreign:
            raise SchemaError(
                f"database {db_path} contains unknown tables {sorted(foreign)}; this is not an agentbus database"
            )
        existed = bool(_tables(db))
        _in_transaction(db, _create_baseline, 1)
        notes.append("stamped existing unversioned database as schema v1" if existed else "created schema v1")
        version = 1
    if version < SCHEMA_VERSION:
        notes.append(f"backup of schema v{version}: {backup(db, db_path, version)}")
        while version < SCHEMA_VERSION:
            _in_transaction(db, MIGRATIONS[version], version + 1)
            version += 1
            notes.append(f"migrated to schema v{version}")
    return notes


def _create_baseline(db: sqlite3.Connection) -> None:
    for statement in _statements(BASELINE):
        db.execute(statement)
    _check_baseline(db)


def _statements(script: str) -> list[str]:
    """Splits an SQL script into statements (`executescript` would commit the surrounding transaction)."""
    out: list[str] = []
    current = ""
    for line in script.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            out.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError(f"incomplete SQL statement: {current.strip()!r}")
    return out


def _in_transaction(db: sqlite3.Connection, step: Migration, new_version: int) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        step(db)
        db.execute(f"PRAGMA user_version = {int(new_version)}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
