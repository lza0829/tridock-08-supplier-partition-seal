from __future__ import annotations
from datetime import datetime, timezone
import sqlite3

from app.db import write_tx
from app.fingerprint import batch_fingerprint
from app.services.gate import sealed_baseline

LEGS = ("expect", "receive", "confirm")
LEG_TO_COLUMN = {"expect": "expect_qty", "receive": "receive_qty", "confirm": "confirm_qty"}
FATES = {"pending", "accept_short", "accept_over", "quarantine"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}


def _snapshot(conn: sqlite3.Connection, batch_id: int, stage: str, note: str | None = None) -> str:
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise ValueError("批次不存在")
    fp = batch_fingerprint(conn, batch_id)
    assert fp is not None
    conn.execute(
        '''INSERT INTO batch_snapshots(batch_id, batch_no, stage, fingerprint, captured_at, note)
           VALUES (?,?,?,?,?,?)''',
        (batch_id, batch["batch_no"], stage, fp, _now(), note),
    )
    return fp


def _sealed_snapshot(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batch_snapshots WHERE batch_id=? AND stage='sealed' ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


def ensure_sealed_snapshot(conn: sqlite3.Connection, batch_id: int) -> str:
    """Return the sealed fingerprint of a batch, backfilling it once for legacy rows."""
    snap = _sealed_snapshot(conn, batch_id)
    if snap:
        return snap["fingerprint"]
    return _snapshot(conn, batch_id, "sealed", "封存指纹补录（订正开批时）")


def verify_against_sealed(conn: sqlite3.Connection, batch_id: int) -> tuple[bool, str | None, str | None]:
    """Compare a batch's live fingerprint against the fingerprint it was sealed with."""
    snap = _sealed_snapshot(conn, batch_id)
    if not snap:
        return False, None, None
    current = batch_fingerprint(conn, batch_id)
    return current == snap["fingerprint"], current, snap["fingerprint"]


def batch_detail(conn: sqlite3.Connection, batch_id: int) -> dict:
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise ValueError("批次不存在")
    current = batch_fingerprint(conn, batch_id)
    snap = _sealed_snapshot(conn, batch_id)
    pending = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'", (batch_id,)
    ).fetchone()["c"]
    correction_count = conn.execute(
        "SELECT COUNT(*) c FROM correction_lines WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    out = dict(batch)
    out["fingerprint"] = current
    out["sealed_fingerprint"] = snap["fingerprint"] if snap else None
    out["intact"] = (current == snap["fingerprint"]) if snap else None
    out["pending_variance"] = pending
    out["correction_count"] = correction_count
    out["related_corrections"] = [
        dict(r)
        for r in conn.execute(
            '''SELECT id, batch_no, status, sealed_at, materialized_at
               FROM reconcile_batches WHERE corrects_batch_id=? ORDER BY id''',
            (batch_id,),
        ).fetchall()
    ]
    return out


# --------------------------------------------------------------------------- #
# baseline batches
# --------------------------------------------------------------------------- #

def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    with write_tx(conn):
        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账；如需更正请开订正批")

        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open' AND kind='baseline'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM batch_snapshots WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM reconcile_batches WHERE id=?", (r["id"],))

        expect_m = _sum_map(conn, "expect_lines", dock_day)
        receive_m = _sum_map(conn, "receive_lines", dock_day)
        confirm_m = _sum_map(conn, "confirm_lines", dock_day)
        keys = set(expect_m) | set(receive_m) | set(confirm_m)

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status,
                                             sealed_at, note, kind, materialized_at)
               VALUES (?,?,0,0,'open',NULL,?,'baseline',?)''',
            (batch_no, dock_day, "三联对齐", _now()),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        now = _now()
        for supplier_code, sku in sorted(keys):
            eq = expect_m.get((supplier_code, sku), 0.0)
            rq = receive_m.get((supplier_code, sku), 0.0)
            cq = confirm_m.get((supplier_code, sku), 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty,
                                         receive_qty, confirm_qty, aligned, changed, origin)
                   VALUES (?,?,?,?,?,?,?,?,0,'baseline')''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty,
                                                receive_qty, confirm_qty, fate, created_at, bucket, origin)
                       VALUES (?,?,?,?,?,?,?,'pending',?,?, 'baseline')''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now, _bucket([eq, rq, cq])),
                )

        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, variance, batch_id),
        )
        _snapshot(conn, batch_id, "materialized", "基线三腿物化")
        return {
            "batch_id": batch_id,
            "batch_no": batch_no,
            "dock_day": dock_day,
            "matched_count": matched,
            "variance_count": variance,
            "status": "open",
            "kind": "baseline",
        }


def _legs_vs_rows(conn: sqlite3.Connection, batch: sqlite3.Row) -> set[tuple[str, str]]:
    """Keys where the batch's materialised rows no longer equal the live three legs."""
    day = batch["dock_day"]
    rows = {
        (r["supplier_code"], r["sku"]): r
        for r in conn.execute(
            "SELECT * FROM match_rows WHERE batch_id=?", (batch["id"],)
        ).fetchall()
    }
    sums = {
        "expect": _sum_map(conn, "expect_lines", day),
        "receive": _sum_map(conn, "receive_lines", day),
        "confirm": _sum_map(conn, "confirm_lines", day),
    }
    keys = set(rows) | set(sums["expect"]) | set(sums["receive"]) | set(sums["confirm"])
    stale: set[tuple[str, str]] = set()
    for key in keys:
        row = rows.get(key)
        for leg, col in LEG_TO_COLUMN.items():
            live = sums[leg].get(key, 0.0)
            held = float(row[col]) if row else 0.0
            if live != held:
                stale.add(key)
                break
    return stale


def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    with write_tx(conn):
        batch = conn.execute(
            '''SELECT * FROM reconcile_batches
               WHERE dock_day=? AND status='open' AND kind='baseline'
               ORDER BY id DESC LIMIT 1''',
            (dock_day,),
        ).fetchone()
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")
        stale = _legs_vs_rows(conn, batch)
        if stale:
            sample = "、".join(f"{a}/{b}" for a, b in sorted(stale)[:3])
            raise ValueError(
                f"批次物化后三腿又有变动（{len(stale)} 组，如 {sample}），请先重跑对账再结案"
            )
        now = _now()
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=?",
            (now, batch["id"]),
        )
        _snapshot(conn, batch["id"], "sealed", "基线结案封印")
        return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())


def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    if fate not in FATES:
        raise ValueError("非法差额命运")
    with write_tx(conn):
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)).fetchone()
        if not batch:
            raise ValueError("差额所属批次不存在")
        if batch["status"] == "sealed":
            raise ValueError("已结案批次不可改差额命运")
        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())


# --------------------------------------------------------------------------- #
# correction batches, anchored on the original (sealed) dock_day
# --------------------------------------------------------------------------- #

def _bucket(qtys: list[float], new_key: bool = False) -> str:
    """Bucket a three-leg mismatch by how the odd leg sits against its peers.

    ``leg_low``  — the two agreeing legs are above the deviant one (少一条腿).
    ``leg_high`` — the two agreeing legs are below the deviant one (多一条腿).
    ``mixed``    — all three disagree.
    ``new_key``  — the key only exists in the correction batch.
    """
    if new_key:
        return "new_key"
    if qtys[0] == qtys[1] == qtys[2]:
        return "aligned"
    for i in range(3):
        pair = [qtys[j] for j in range(3) if j != i]
        if pair[0] == pair[1]:
            return "leg_low" if qtys[i] < pair[0] else "leg_high"
    return "mixed"


def _baseline_rows(conn: sqlite3.Connection, batch_id: int) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute("SELECT * FROM match_rows WHERE batch_id=?", (batch_id,)).fetchall()
    return {(r["supplier_code"], r["sku"]): r for r in rows}


def _load_correction(conn: sqlite3.Connection, batch_id: int, *, for_write: bool) -> sqlite3.Row:
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise ValueError("批次不存在")
    if batch["kind"] != "correction":
        if batch["status"] == "sealed":
            raise ValueError(
                f"批次 {batch['batch_no']} 是已封印的基线批，明细只读；请在订正批中写订正"
            )
        raise ValueError("该批次不是订正批，不能写订正行")
    if for_write and batch["status"] != "open":
        raise ValueError(f"订正批 {batch['batch_no']} 已结案，明细只读")
    if not batch["corrects_batch_id"]:
        raise ValueError("订正批缺少原批关联，拒绝操作")
    return batch


def open_correction_batch(conn: sqlite3.Connection, dock_day: str, note: str | None = None) -> dict:
    with write_tx(conn):
        baseline = sealed_baseline(conn, dock_day)
        if not baseline:
            raise ValueError(f"到货日 {dock_day} 尚未结案，请直接改原批后对账，无需开订正批")
        existing = conn.execute(
            '''SELECT * FROM reconcile_batches
               WHERE dock_day=? AND kind='correction' AND status='open'
               ORDER BY id DESC LIMIT 1''',
            (dock_day,),
        ).fetchone()
        if existing:
            return {**dict(existing), "reused": True}
        ensure_sealed_snapshot(conn, baseline["id"])
        n = conn.execute(
            "SELECT COUNT(*) c FROM reconcile_batches WHERE dock_day=?", (dock_day,)
        ).fetchone()["c"] + 1
        batch_no = f"CB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status,
                                             sealed_at, note, kind, corrects_batch_id, corrects_batch_no)
               VALUES (?,?,0,0,'open',NULL,?,'correction',?,?)''',
            (batch_no, dock_day, note or "结案后发现错数", baseline["id"], baseline["batch_no"]),
        )
        batch_id = int(cur.lastrowid)
        _snapshot(conn, batch_id, "opened", "订正批开批")
        return {**dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()),
                "reused": False}


def _prior_deltas(
    conn: sqlite3.Connection, dock_day: str, exclude_batch_id: int
) -> dict[tuple[str, str], dict[str, float]]:
    """Deltas already sealed by earlier correction batches of the same day.

    A second correction after a first one has been closed must start from the
    closed batch's quantities, otherwise the trail of corrections would silently
    drift away from what everyone already signed off on.
    """
    rows = conn.execute(
        '''SELECT c.supplier_code, c.sku, c.leg, SUM(c.delta) d
           FROM correction_lines c
           JOIN reconcile_batches b ON b.id = c.batch_id
           WHERE b.dock_day=? AND b.status='sealed' AND b.kind='correction'
             AND b.id <> ?
           GROUP BY c.supplier_code, c.sku, c.leg''',
        (dock_day, exclude_batch_id),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        out.setdefault((r["supplier_code"], r["sku"]), {})[r["leg"]] = float(r["d"])
    return out


def _effective_prev(
    conn: sqlite3.Connection,
    batch: sqlite3.Row,
    key: tuple[str, str],
    leg: str,
    prior: dict[tuple[str, str], dict[str, float]] | None = None,
) -> float:
    baseline_rows = _baseline_rows(conn, int(batch["corrects_batch_id"]))
    base = baseline_rows.get(key)
    value = float(base[LEG_TO_COLUMN[leg]]) if base else 0.0
    if prior is None:
        prior = _prior_deltas(conn, batch["dock_day"], int(batch["id"]))
    sealed_delta = prior.get(key, {}).get(leg, 0.0)
    recorded = conn.execute(
        '''SELECT COALESCE(SUM(delta),0) d FROM correction_lines
           WHERE batch_id=? AND supplier_code=? AND sku=? AND leg=?''',
        (batch["id"], key[0], key[1], leg),
    ).fetchone()["d"]
    return value + sealed_delta + float(recorded)


def add_correction_line(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    correction_no: str,
    supplier_code: str,
    sku: str,
    leg: str,
    new_qty: float,
    reason: str | None = None,
    note: str | None = None,
) -> dict:
    if leg not in LEGS:
        raise ValueError("订正腿必须是 expect/receive/confirm 之一")
    if new_qty < 0:
        raise ValueError("订正后数量不能为负")
    with write_tx(conn):
        batch = _load_correction(conn, batch_id, for_write=True)
        baseline = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch["corrects_batch_id"],)
        ).fetchone()
        if not baseline or baseline["status"] != "sealed":
            raise ValueError("原批未封印，订正批不可写；原封印不可用则拒绝订正")
        key = (supplier_code, sku)
        prior = _prior_deltas(conn, batch["dock_day"], batch_id)
        prev = _effective_prev(conn, batch, key, leg, prior)
        delta = new_qty - prev
        conn.execute(
            '''INSERT INTO correction_lines(correction_no, batch_id, dock_day, supplier_code, sku,
                                            leg, prev_qty, new_qty, delta, reason, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (correction_no, batch_id, batch["dock_day"], supplier_code, sku, leg,
             prev, new_qty, delta, reason, note, _now()),
        )
        # Any new correction invalidates the previous materialisation of this batch.
        conn.execute("DELETE FROM match_rows WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM batch_snapshots WHERE batch_id=? AND stage='materialized'", (batch_id,))
        conn.execute(
            "UPDATE reconcile_batches SET materialized_at=NULL, matched_count=0, variance_count=0 WHERE id=?",
            (batch_id,),
        )
        return dict(conn.execute(
            "SELECT * FROM correction_lines WHERE id=last_insert_rowid()"
        ).fetchone())


def list_corrections(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM correction_lines WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
    ]


def materialize_correction(conn: sqlite3.Connection, batch_id: int) -> dict:
    with write_tx(conn):
        batch = _load_correction(conn, batch_id, for_write=True)
        baseline = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch["corrects_batch_id"],)
        ).fetchone()
        if not baseline or baseline["status"] != "sealed":
            raise ValueError("原批已不是封印态，拒绝在失去对照的情况下物化订正")
        intact, current, sealed_fp = verify_against_sealed(conn, int(batch["corrects_batch_id"]))
        if not intact:
            raise ValueError(
                "原封印批指纹已变（原批可能被改脏），拒绝物化订正："
                f"现行 {current} / 封印 {sealed_fp}"
            )

        lines = conn.execute(
            "SELECT * FROM correction_lines WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
        if not lines:
            raise ValueError("订正批没有订正行，无法物化")

        deltas: dict[tuple[str, str], dict[str, float]] = {}
        for line in lines:
            d = deltas.setdefault((line["supplier_code"], line["sku"]), {})
            d[line["leg"]] = d.get(line["leg"], 0.0) + float(line["delta"])

        # The starting point of a correction is the latest *sealed* state of the
        # day: the newest sealed correction batch if one exists, else the baseline.
        # Its materialised rows already fold in every earlier correction.
        carry_batch = conn.execute(
            '''SELECT id FROM reconcile_batches
               WHERE dock_day=? AND kind='correction' AND status='sealed' AND id <> ?
               ORDER BY id DESC LIMIT 1''',
            (batch["dock_day"], batch_id),
        ).fetchone()
        carry_source = int(carry_batch["id"]) if carry_batch else int(batch["corrects_batch_id"])
        base_rows = _baseline_rows(conn, int(batch["corrects_batch_id"]))
        carry_rows = _baseline_rows(conn, carry_source)
        prior_variances = {
            (r["supplier_code"], r["sku"]): r
            for r in conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=?", (carry_source,)
            ).fetchall()
        }
        keys = set(carry_rows) | set(deltas)

        conn.execute("DELETE FROM match_rows WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (batch_id,))

        matched = 0
        variance = 0
        pending = 0
        now = _now()
        for supplier_code, sku in sorted(keys):
            carry = carry_rows.get((supplier_code, sku))
            new_key = (supplier_code, sku) not in base_rows and carry is None
            d = deltas.get((supplier_code, sku), {})
            eq = (float(carry["expect_qty"]) if carry else 0.0) + d.get("expect", 0.0)
            rq = (float(carry["receive_qty"]) if carry else 0.0) + d.get("receive", 0.0)
            cq = (float(carry["confirm_qty"]) if carry else 0.0) + d.get("confirm", 0.0)
            qtys = [eq, rq, cq]
            aligned = 1 if eq == rq == cq else 0
            changed = 1 if d else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty,
                                         receive_qty, confirm_qty, aligned, changed, origin)
                   VALUES (?,?,?,?,?,?,?,?,?, 'correction')''',
                (batch_id, batch["dock_day"], supplier_code, sku, eq, rq, cq, aligned, changed),
            )
            if aligned:
                matched += 1
                continue
            variance += 1
            prior = prior_variances.get((supplier_code, sku))
            if changed or prior is None:
                fate, origin, carried = "pending", "correction", None
            else:
                # Untouched key: carry the original disposition forward so the
                # correction batch only needs to resolve what the correction moved.
                fate, origin = prior["fate"], "carried"
                carried = prior["id"]
            if fate == "pending":
                pending += 1
            conn.execute(
                '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty,
                                            receive_qty, confirm_qty, fate, created_at, bucket, origin,
                                            carried_from_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (batch_id, batch["dock_day"], supplier_code, sku, eq, rq, cq, fate, now,
                 _bucket(qtys, new_key=new_key), origin, carried),
            )

        conn.execute(
            '''UPDATE reconcile_batches
               SET matched_count=?, variance_count=?, materialized_at=? WHERE id=?''',
            (matched, variance, now, batch_id),
        )
        fp = _snapshot(conn, batch_id, "materialized", "订正物化")
        return {
            "batch_id": batch_id,
            "batch_no": batch["batch_no"],
            "matched_count": matched,
            "variance_count": variance,
            "pending_variance": pending,
            "fingerprint": fp,
        }


def seal_correction_batch(conn: sqlite3.Connection, batch_id: int) -> dict:
    with write_tx(conn):
        batch = _load_correction(conn, batch_id, for_write=False)
        if batch["status"] != "open":
            raise ValueError(f"订正批 {batch['batch_no']} 已结案")
        # The correction only counts if the original seal is still intact, so the
        # fingerprint check runs before any business gate.
        baseline = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch["corrects_batch_id"],)
        ).fetchone()
        if not baseline or baseline["status"] != "sealed":
            raise ValueError("原批封印状态丢失，拒绝结案订正批")
        intact, current, sealed_fp = verify_against_sealed(conn, int(batch["corrects_batch_id"]))
        if not intact:
            raise ValueError(
                "原封印批指纹已变，订正成功但无法对照原封印，拒绝结案："
                f"现行 {current} / 封印 {sealed_fp}"
            )
        if not batch["materialized_at"]:
            raise ValueError("订正批尚未物化对齐，不能结案")
        count = conn.execute(
            "SELECT COUNT(*) c FROM correction_lines WHERE batch_id=?", (batch_id,)
        ).fetchone()["c"]
        if not count:
            raise ValueError("订正批没有订正行，不能结案")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'", (batch_id,)
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条订正差额未处置，不能结案")

        now = _now()
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=?",
            (now, batch_id),
        )
        _snapshot(conn, batch_id, "sealed", f"订正结案，对照 {baseline['batch_no']}")
        return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone())


def snapshots(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM batch_snapshots WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
    ]


def day_batches(conn: sqlite3.Connection, dock_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id", (dock_day,)
    ).fetchall()
    out = []
    for r in rows:
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'", (r["id"],)
        ).fetchone()["c"]
        d = dict(r)
        d["pending_variance"] = pending
        out.append(d)
    return out
