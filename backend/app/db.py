from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import DATA_DIR, DB_PATH


def connect() -> sqlite3.Connection:
    """Open an autocommit connection.

    Write paths must use :func:`write_tx` so that read-only gates and the
    writes they protect happen inside one ``BEGIN IMMEDIATE`` critical section.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction.

    ``BEGIN IMMEDIATE`` takes the write lock before the sealed/read-only check,
    so a concurrent seal and a concurrent line write can never interleave
    half-way: one of them observes the other's committed state.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")
