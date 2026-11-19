from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services import reconcile_service as svc
from app.services.gate import is_sealed

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard(dock_day: str | None = None):
    conn = connect()
    try:
        day = dock_day
        if not day:
            day = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()["value"]
        expect_n = conn.execute("SELECT COUNT(*) c FROM expect_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        receive_n = conn.execute("SELECT COUNT(*) c FROM receive_lines WHERE dock_day=?", (day,)).fetchone()["c"]
        confirm_n = conn.execute("SELECT COUNT(*) c FROM confirm_lines WHERE dock_day=?", (day,)).fetchone()["c"]

        baseline = conn.execute(
            '''SELECT * FROM reconcile_batches
               WHERE dock_day=? AND kind='baseline' ORDER BY id DESC LIMIT 1''',
            (day,),
        ).fetchone()
        correction = conn.execute(
            '''SELECT * FROM reconcile_batches
               WHERE dock_day=? AND kind='correction' ORDER BY id DESC LIMIT 1''',
            (day,),
        ).fetchone()

        def enrich(batch):
            if not batch:
                return None
            row = svc.batch_detail(conn, batch["id"])
            return {
                "id": row["id"],
                "batch_no": row["batch_no"],
                "kind": row["kind"],
                "status": row["status"],
                "sealed_at": row["sealed_at"],
                "matched_count": row["matched_count"],
                "variance_count": row["variance_count"],
                "pending_variance": row["pending_variance"],
                "correction_count": row["correction_count"],
                "corrects_batch_id": row["corrects_batch_id"],
                "corrects_batch_no": row["corrects_batch_no"],
                "materialized_at": row["materialized_at"],
                "fingerprint": row["fingerprint"],
                "sealed_fingerprint": row["sealed_fingerprint"],
                "intact": row["intact"],
            }

        base = enrich(baseline)
        corr = enrich(correction)
        effective = corr or base
        return {
            "dock_day": day,
            "sealed": is_sealed(conn, day),
            "expect_count": expect_n,
            "receive_count": receive_n,
            "confirm_count": confirm_n,
            "baseline_batch": base,
            "correction_batch": corr,
            "latest_batch": effective,
            "pending_variance": effective["pending_variance"] if effective else 0,
            "baseline_intact": base["intact"] if base else None,
        }
    finally:
        conn.close()
