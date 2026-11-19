from __future__ import annotations
import sqlite3


def is_sealed(conn: sqlite3.Connection, dock_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='sealed' LIMIT 1",
        (dock_day,),
    ).fetchone()
    return row is not None


def sealed_baseline(conn: sqlite3.Connection, dock_day: str) -> sqlite3.Row | None:
    """The sealed baseline batch for a day, if the day has been closed."""
    return conn.execute(
        '''SELECT * FROM reconcile_batches
           WHERE dock_day=? AND status='sealed' AND kind='baseline'
           ORDER BY id DESC LIMIT 1''',
        (dock_day,),
    ).fetchone()


def assert_writable(conn: sqlite3.Connection, dock_day: str) -> None:
    """A sealed day's three legs are frozen.

    Call this *inside* the same ``BEGIN IMMEDIATE`` transaction that performs
    the write (see ``app.db.write_tx``), never before it, otherwise a concurrent
    seal can slip between the check and the insert.
    """
    if is_sealed(conn, dock_day):
        raise ValueError(f"到货日 {dock_day} 已结案，禁止再改预报/实收/回执")
