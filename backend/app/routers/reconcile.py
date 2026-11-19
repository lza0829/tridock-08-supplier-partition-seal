from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import connect
from app.services import reconcile_service as svc

router = APIRouter(tags=["reconcile"])


class RunIn(BaseModel):
    dock_day: str


class SealIn(BaseModel):
    dock_day: str


class FateIn(BaseModel):
    fate: str


class OpenCorrectionIn(BaseModel):
    dock_day: str
    note: str | None = None


class CorrectionIn(BaseModel):
    batch_id: int
    correction_no: str
    supplier_code: str
    sku: str
    leg: str
    new_qty: float = Field(ge=0)
    reason: str | None = None
    note: str | None = None


def _run(fn, *args, **kwargs):
    conn = connect()
    try:
        return fn(conn, *args, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.post("/reconcile/run")
def run_reconcile(body: RunIn):
    return _run(svc.run_reconcile, body.dock_day)


@router.post("/reconcile/seal")
def seal(body: SealIn):
    return _run(svc.seal_day, body.dock_day)


@router.get("/reconcile/batches")
def batches(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            return svc.day_batches(conn, dock_day)
        rows = conn.execute("SELECT * FROM reconcile_batches ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reconcile/batches/{batch_id}")
def batch_detail(batch_id: int):
    return _run(svc.batch_detail, batch_id)


@router.get("/reconcile/batches/{batch_id}/snapshots")
def batch_snapshots(batch_id: int):
    return _run(svc.snapshots, batch_id)


@router.get("/reconcile/batches/{batch_id}/verify")
def batch_verify(batch_id: int):
    def _verify(conn, bid):
        detail = svc.batch_detail(conn, bid)
        return {
            "batch_id": bid,
            "batch_no": detail["batch_no"],
            "status": detail["status"],
            "kind": detail["kind"],
            "fingerprint": detail["fingerprint"],
            "sealed_fingerprint": detail["sealed_fingerprint"],
            "intact": detail["intact"],
        }

    return _run(_verify, batch_id)


@router.get("/reconcile/matches")
def matches(batch_id: int):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/reconcile/variances")
def variances(batch_id: int | None = None, dock_day: str | None = None):
    conn = connect()
    try:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        elif dock_day:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/reconcile/variances/{variance_id}/fate")
def set_fate(variance_id: int, body: FateIn):
    return _run(svc.set_variance_fate, variance_id, body.fate)


@router.post("/reconcile/corrections/open")
def open_correction(body: OpenCorrectionIn):
    return _run(svc.open_correction_batch, body.dock_day, body.note)


@router.get("/reconcile/corrections")
def corrections(batch_id: int):
    return _run(svc.list_corrections, batch_id)


@router.post("/reconcile/corrections")
def add_correction(body: CorrectionIn):
    return _run(
        svc.add_correction_line,
        body.batch_id,
        correction_no=body.correction_no,
        supplier_code=body.supplier_code,
        sku=body.sku,
        leg=body.leg,
        new_qty=body.new_qty,
        reason=body.reason,
        note=body.note,
    )


@router.post("/reconcile/corrections/{batch_id}/materialize")
def materialize_correction(batch_id: int):
    return _run(svc.materialize_correction, batch_id)


@router.post("/reconcile/corrections/{batch_id}/seal")
def seal_correction(batch_id: int):
    return _run(svc.seal_correction_batch, batch_id)
