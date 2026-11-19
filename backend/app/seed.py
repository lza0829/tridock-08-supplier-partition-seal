from __future__ import annotations
import sqlite3

from app.db import connect

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expect_lines (
  id INTEGER PRIMARY KEY,
  doc_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receive_lines (
  id INTEGER PRIMARY KEY,
  receive_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirm_lines (
  id INTEGER PRIMARY KEY,
  confirm_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  dock_day TEXT NOT NULL,
  matched_count INTEGER NOT NULL,
  variance_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  sealed_at TEXT,
  note TEXT,
  kind TEXT NOT NULL DEFAULT 'baseline',
  corrects_batch_id INTEGER,
  corrects_batch_no TEXT,
  materialized_at TEXT
);
CREATE TABLE IF NOT EXISTS match_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  aligned INTEGER NOT NULL,
  changed INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'baseline'
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  fate TEXT NOT NULL,
  created_at TEXT NOT NULL,
  bucket TEXT NOT NULL DEFAULT 'three_way',
  origin TEXT NOT NULL DEFAULT 'baseline',
  carried_from_id INTEGER
);
CREATE TABLE IF NOT EXISTS correction_lines (
  id INTEGER PRIMARY KEY,
  correction_no TEXT NOT NULL UNIQUE,
  batch_id INTEGER NOT NULL REFERENCES reconcile_batches(id) ON DELETE RESTRICT,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  leg TEXT NOT NULL,
  prev_qty REAL NOT NULL,
  new_qty REAL NOT NULL,
  delta REAL NOT NULL,
  reason TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_snapshots (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES reconcile_batches(id) ON DELETE CASCADE,
  batch_no TEXT NOT NULL,
  stage TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS late_spill (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_match_batch ON match_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_var_batch ON variance_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_corr_batch ON correction_lines(batch_id);
CREATE INDEX IF NOT EXISTS idx_snap_batch ON batch_snapshots(batch_id);
'''

# Columns added after the first schema version.  Every entry is (table, column, ddl).
MIGRATIONS: list[tuple[str, str, str]] = [
    ("reconcile_batches", "kind", "TEXT NOT NULL DEFAULT 'baseline'"),
    ("reconcile_batches", "corrects_batch_id", "INTEGER"),
    ("reconcile_batches", "corrects_batch_no", "TEXT"),
    ("reconcile_batches", "materialized_at", "TEXT"),
    ("match_rows", "changed", "INTEGER NOT NULL DEFAULT 0"),
    ("match_rows", "origin", "TEXT NOT NULL DEFAULT 'baseline'"),
    ("variance_rows", "bucket", "TEXT NOT NULL DEFAULT 'three_way'"),
    ("variance_rows", "origin", "TEXT NOT NULL DEFAULT 'baseline'"),
    ("variance_rows", "carried_from_id", "INTEGER"),
]

# Defence in depth: even a code path that forgets its gate cannot mutate a
# sealed batch's detail rows.  The app-level gate runs first; these triggers are
# the last line that keeps "sealed" true under concurrency and bugs.
GUARDS = '''
CREATE TRIGGER IF NOT EXISTS trg_match_rows_sealed_ins
BEFORE INSERT ON match_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=NEW.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_match_rows_sealed_upd
BEFORE UPDATE ON match_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_match_rows_sealed_del
BEFORE DELETE ON match_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_variance_rows_sealed_ins
BEFORE INSERT ON variance_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=NEW.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_variance_rows_sealed_upd
BEFORE UPDATE ON variance_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_variance_rows_sealed_del
BEFORE DELETE ON variance_rows
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_correction_lines_sealed_ins
BEFORE INSERT ON correction_lines
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=NEW.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_correction_lines_sealed_upd
BEFORE UPDATE ON correction_lines
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_correction_lines_sealed_del
BEFORE DELETE ON correction_lines
FOR EACH ROW WHEN (SELECT status FROM reconcile_batches WHERE id=OLD.batch_id)='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch detail is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_batches_sealed_upd
BEFORE UPDATE ON reconcile_batches
FOR EACH ROW WHEN OLD.status='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch is read-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_batches_sealed_del
BEFORE DELETE ON reconcile_batches
FOR EACH ROW WHEN OLD.status='sealed'
BEGIN SELECT RAISE(ABORT, 'sealed batch is read-only'); END;
'''


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.executescript(GUARDS)


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        if conn.execute("SELECT COUNT(*) c FROM suppliers").fetchone()["c"]:
            return
        conn.execute("INSERT INTO settings(key, value) VALUES ('dock_day', ?)", ("2026-11-18",))
        suppliers = [
            ("SUP-A", "东海食材"),
            ("SUP-B", "北岭包装"),
            ("SUP-C", "南湾百货"),
        ]
        for code, title in suppliers:
            conn.execute("INSERT INTO suppliers(code, title) VALUES (?,?)", (code, title))
        expects = [
            ("EX-1001", "SUP-A", "SKU-米-25", "2026-11-18", 40, "早班预报"),
            ("EX-1002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("EX-1003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            ("EX-1004", "SUP-C", "SKU-巾-卷", "2026-11-18", 60, None),
            ("EX-0901", "SUP-A", "SKU-米-25", "2026-11-17", 10, "昨日"),
        ]
        for row in expects:
            conn.execute(
                '''INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at)
                   VALUES (?,?,?,?,?,?,?)''',
                (*row, "2026-11-18T08:00:00Z"),
            )
        receives = [
            ("RC-2001", "SUP-A", "SKU-米-25", "2026-11-18", 38, "2026-11-18T09:10:00Z", "少2"),
            ("RC-2002", "SUP-A", "SKU-油-5", "2026-11-18", 20, "2026-11-18T09:20:00Z", None),
            ("RC-2003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, "2026-11-18T10:00:00Z", None),
            ("RC-2004", "SUP-C", "SKU-巾-卷", "2026-11-18", 66, "2026-11-18T11:00:00Z", "多6"),
        ]
        for row in receives:
            conn.execute(
                '''INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[5]),
            )
        confirms = [
            ("CF-3001", "SUP-A", "SKU-米-25", "2026-11-18", 38, "回执按实收"),
            ("CF-3002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("CF-3003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            ("CF-3004", "SUP-C", "SKU-巾-卷", "2026-11-18", 60, "回执仍报预报数"),
        ]
        for row in confirms:
            conn.execute(
                '''INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at)
                   VALUES (?,?,?,?,?,?,?)''',
                (*row, "2026-11-18T12:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
