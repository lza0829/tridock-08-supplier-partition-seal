from __future__ import annotations
"""密封批指纹（快照）计算。

指纹只覆盖某个批次自身的行集，不覆盖同日的其他批次：
原 sealed 批的 match_rows / variance_rows 与订正批的行集互不重叠，
因此订正批的任何写入都不会改变原批指纹。
"""
import hashlib
import json
import sqlite3
from typing import Any

BATCH_FIELDS = (
    "id",
    "batch_no",
    "dock_day",
    "matched_count",
    "variance_count",
    "status",
    "sealed_at",
    "kind",
    "corrects_batch_id",
    "corrects_batch_no",
    "materialized_at",
)
MATCH_FIELDS = (
    "id",
    "dock_day",
    "supplier_code",
    "sku",
    "expect_qty",
    "receive_qty",
    "confirm_qty",
    "aligned",
    "changed",
    "origin",
)
CORRECTION_FIELDS = (
    "id",
    "correction_no",
    "supplier_code",
    "sku",
    "leg",
    "prev_qty",
    "new_qty",
    "delta",
    "reason",
    "note",
    "created_at",
)
VARIANCE_FIELDS = (
    "id",
    "dock_day",
    "supplier_code",
    "sku",
    "expect_qty",
    "receive_qty",
    "confirm_qty",
    "fate",
    "bucket",
    "origin",
    "created_at",
)

_FLOAT_FIELDS = {
    "expect_qty",
    "receive_qty",
    "confirm_qty",
    "prev_qty",
    "new_qty",
    "delta",
}
_INT_FIELDS = {"aligned", "changed", "matched_count", "variance_count"}


def _normalize(fields: tuple[str, ...], row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    keys = row.keys()
    for field in fields:
        if field not in keys:
            out[field] = None
            continue
        value = row[field]
        if field in _FLOAT_FIELDS and value is not None:
            value = float(value)
        elif field in _INT_FIELDS and value is not None:
            value = int(value)
        out[field] = value
    return out


def batch_fingerprint(conn: sqlite3.Connection, batch_id: int) -> str | None:
    batch = conn.execute(
        "SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        return None
    matches = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()
    variances = conn.execute(
        "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()
    correction_lines = conn.execute(
        "SELECT * FROM correction_lines WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()
    payload = {
        "batch": _normalize(BATCH_FIELDS, batch),
        "matches": [_normalize(MATCH_FIELDS, r) for r in matches],
        "variances": [_normalize(VARIANCE_FIELDS, r) for r in variances],
        "corrections": [_normalize(CORRECTION_FIELDS, r) for r in correction_lines],
    }
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
