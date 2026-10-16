from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentbus import schema
from agentbus.schema import BASELINE, SCHEMA_VERSION, SchemaError
from agentbus.store import Store


def version_of(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def make_unversioned_db(path: Path) -> None:
    """A database as written by agentbus before schema versioning: baseline tables, user_version 0, some data."""
    db = sqlite3.connect(path)
    db.executescript(BASELINE)
    db.execute("INSERT INTO agents(name, role, registered_at, last_seen) VALUES ('director', 'lead', 1, 1)")
    db.execute("INSERT INTO agents(name, role, registered_at, last_seen) VALUES ('api', 'x', 1, 1)")
    db.execute("""INSERT INTO messages(ts, sender, recipient, kind, priority, body)
                  VALUES (1, 'director', 'api', 'note', 'normal', 'kept across the upgrade')""")
    db.commit()
    db.close()


def test_new_database_gets_the_current_version(tmp_path: Path) -> None:
    store = Store(tmp_path / "sub" / "bus.db")
    assert store.migration_notes == ["created schema v1"]
    store.close()
    assert version_of(tmp_path / "sub" / "bus.db") == SCHEMA_VERSION


def test_unversioned_database_is_stamped_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "bus.db"
    make_unversioned_db(path)
    store = Store(path)
    assert store.migration_notes == ["stamped existing unversioned database as schema v1"]
    assert [a["name"] for a in store.agents()] == ["api", "director"]
    assert store.inbox("api")["messages"][0]["body"] == "kept across the upgrade"
    store.close()
    assert version_of(path) == 1
    assert not list(tmp_path.glob("*.bak"))  # stamping changes no data, so no backup is needed


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bus.db"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaError, match="upgrade agentbus"):
        Store(path)


def test_foreign_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE invoices (id INTEGER)")
    with pytest.raises(SchemaError, match="not an agentbus database"):
        Store(path)


def test_baseline_tables_with_other_columns_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "bus.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE agents (name TEXT PRIMARY KEY)")
    with pytest.raises(SchemaError, match="table 'agents' has columns"):
        Store(path)
    assert version_of(path) == 0  # the failed stamping was rolled back


def test_migrations_run_in_order_after_a_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bus.db"
    Store(path).close()

    def add_column(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE agents ADD COLUMN color TEXT NOT NULL DEFAULT ''")

    def add_table(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(schema, "MIGRATIONS", {1: add_column, 2: add_table})
    store = Store(path)
    assert store.migration_notes == [
        f"backup of schema v1: {tmp_path / 'bus.db.v1.bak'}",
        "migrated to schema v2",
        "migrated to schema v3",
    ]
    store.close()
    assert version_of(path) == 3
    assert version_of(tmp_path / "bus.db.v1.bak") == 1
    with sqlite3.connect(path) as db:
        assert "color" in {r[1] for r in db.execute("PRAGMA table_info(agents)")}
        assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_failed_migration_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bus.db"
    store = Store(path)
    store.register("api", "x")
    store.close()

    def broken(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM agents")
        raise RuntimeError("bug in migration")

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(schema, "MIGRATIONS", {1: broken})
    with pytest.raises(RuntimeError, match="bug in migration"):
        Store(path)
    assert version_of(path) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agents").fetchone()[0] == 1


def test_statement_splitter() -> None:
    statements = schema._statements("CREATE TABLE a (x TEXT DEFAULT ';');\n-- note\nCREATE INDEX i ON a(x);\n")
    assert statements == ["CREATE TABLE a (x TEXT DEFAULT ';');", "-- note\nCREATE INDEX i ON a(x);"]
    with pytest.raises(ValueError, match="incomplete"):
        schema._statements("CREATE TABLE b (x")


def test_file_that_is_not_sqlite_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bus.db"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(SchemaError, match="cannot use"):
        Store(path)
