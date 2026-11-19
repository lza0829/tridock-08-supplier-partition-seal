from __future__ import annotations
import sqlite3

from app.seed import GUARDS, MIGRATIONS, SCHEMA, _columns, migrate

LEGACY = '''
CREATE TABLE reconcile_batches (
  id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, dock_day TEXT NOT NULL,
  matched_count INTEGER NOT NULL, variance_count INTEGER NOT NULL,
  status TEXT NOT NULL, sealed_at TEXT, note TEXT
);
CREATE TABLE match_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL, sku TEXT NOT NULL,
  expect_qty REAL NOT NULL, receive_qty REAL NOT NULL, confirm_qty REAL NOT NULL,
  aligned INTEGER NOT NULL
);
CREATE TABLE variance_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL, sku TEXT NOT NULL,
  expect_qty REAL NOT NULL, receive_qty REAL NOT NULL, confirm_qty REAL NOT NULL,
  fate TEXT NOT NULL, created_at TEXT NOT NULL
);
INSERT INTO reconcile_batches(batch_no,dock_day,matched_count,variance_count,status,sealed_at,note)
  VALUES('RB-OLD-001','2026-11-18',1,1,'sealed','2026-11-18T23:00:00Z','旧封印');
INSERT INTO match_rows(batch_id,dock_day,supplier_code,sku,expect_qty,receive_qty,confirm_qty,aligned)
  VALUES(1,'2026-11-18','SUP-A','SKU-1',10,8,8,0);
INSERT INTO variance_rows(batch_id,dock_day,supplier_code,sku,expect_qty,receive_qty,confirm_qty,fate,created_at)
  VALUES(1,'2026-11-18','SUP-A','SKU-1',10,8,8,'accept_short','2026-11-18T23:00:00Z');
'''


def test_legacy_database_is_migrated_and_guarded(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY)
    conn.executescript(SCHEMA)
    migrate(conn)

    cols = _columns(conn, "reconcile_batches")
    assert {"kind", "corrects_batch_id", "corrects_batch_no", "materialized_at"} <= cols
    assert {"changed", "origin"} <= _columns(conn, "match_rows")
    assert {"bucket", "origin", "carried_from_id"} <= _columns(conn, "variance_rows")

    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=1").fetchone()
    assert batch["kind"] == "baseline"  # legacy sealed rows default to baseline

    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_match_rows_sealed_upd" in triggers

    try:
        conn.execute("UPDATE match_rows SET receive_qty=99 WHERE batch_id=1")
        raise AssertionError("guard did not fire")
    except sqlite3.DatabaseError as exc:
        assert "read-only" in str(exc)

    # migration is idempotent
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM batch_snapshots").fetchone()[0] == 0
    conn.close()


def test_guards_are_installed_on_fresh_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(GUARDS)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert len(names) >= 11
    for table, column, _ddl in MIGRATIONS:
        assert column in _columns(conn, table)
    conn.close()
