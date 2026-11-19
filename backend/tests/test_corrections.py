from __future__ import annotations
import sqlite3
import threading
import time

import pytest

from app.db import connect

DAY = "2026-11-18"


def _close_baseline_day(client):
    r = client.post("/api/reconcile/run", json={"dock_day": DAY})
    assert r.status_code == 200, r.text
    batch = r.json()
    for v in client.get(f"/api/reconcile/variances?batch_id={batch['batch_id']}").json():
        client.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"})
    r = client.post("/api/reconcile/seal", json={"dock_day": DAY})
    assert r.status_code == 200, r.text
    return client.get("/api/dashboard").json()["baseline_batch"]


def _open_correction(client):
    r = client.post("/api/reconcile/corrections/open", json={"dock_day": DAY, "note": "米少记2"})
    assert r.status_code == 200, r.text
    return r.json()


def _dispose(client, batch_id, fate="accept_short"):
    for v in client.get(f"/api/reconcile/variances?batch_id={batch_id}").json():
        if v["fate"] == "pending":
            r = client.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": fate})
            assert r.status_code == 200, r.text


def _correct(client, batch_id, no, sku, leg, new_qty, supplier="SUP-A"):
    return client.post(
        "/api/reconcile/corrections",
        json={
            "batch_id": batch_id,
            "correction_no": no,
            "supplier_code": supplier,
            "sku": sku,
            "leg": leg,
            "new_qty": new_qty,
            "reason": "错数",
        },
    )


def test_sealed_baseline_fingerprint_never_changes(client):
    base = _close_baseline_day(client)
    before = client.get(f"/api/reconcile/batches/{base['id']}/verify").json()
    assert before["intact"] is True

    corr = _open_correction(client)
    assert corr["corrects_batch_no"] == base["batch_no"]
    assert _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40).status_code == 200
    assert client.post(f"/api/reconcile/corrections/{corr['id']}/materialize").status_code == 200
    _dispose(client, corr["id"])
    assert client.post(f"/api/reconcile/corrections/{corr['id']}/seal").status_code == 200

    after = client.get(f"/api/reconcile/batches/{base['id']}/verify").json()
    assert after["fingerprint"] == before["fingerprint"]
    assert after["intact"] is True
    # the original sealed batch detail is byte-for-byte what it was
    matches = client.get(f"/api/reconcile/matches?batch_id={base['id']}").json()
    rice = [m for m in matches if m["sku"] == "SKU-米-25"][0]
    assert rice["receive_qty"] == 38


def test_correction_batch_is_visible_and_linked(client):
    base = _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")

    dash = client.get("/api/dashboard").json()
    assert dash["baseline_batch"]["batch_no"] == base["batch_no"]
    assert dash["correction_batch"]["batch_no"] == corr["batch_no"]
    assert dash["correction_batch"]["corrects_batch_no"] == base["batch_no"]
    assert dash["latest_batch"]["batch_no"] == corr["batch_no"]

    listed = client.get(f"/api/reconcile/batches?dock_day={DAY}").json()
    kinds = {b["kind"] for b in listed}
    assert kinds == {"baseline", "correction"}
    detail = client.get(f"/api/reconcile/batches/{corr['id']}").json()
    assert detail["corrects_batch_no"] == base["batch_no"]
    assert detail["correction_count"] == 1

    # the corrected quantity shows up in the correction batch, not the baseline
    rows = client.get(f"/api/reconcile/matches?batch_id={corr['id']}").json()
    rice = [m for m in rows if m["sku"] == "SKU-米-25"][0]
    assert rice["receive_qty"] == 40
    assert rice["changed"] == 1
    assert rice["origin"] == "correction"


def test_writing_into_sealed_batch_is_rejected(client):
    base = _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)

    # 1. correction line addressed at the sealed baseline batch
    r = _correct(client, base["id"], "CR-2", "SKU-油-5", "receive", 25)
    assert r.status_code == 400
    assert "只读" in r.json()["detail"] or "订正批" in r.json()["detail"]

    # 2. plain three-leg write on the sealed day
    r = client.post(
        "/api/expects",
        json={"doc_no": "EX-9", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": DAY, "qty": 1},
    )
    assert r.status_code == 400
    assert "已结案" in r.json()["detail"]

    # 3. direct DB tampering is stopped by the schema guards
    conn = connect()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE match_rows SET receive_qty=999 WHERE batch_id=?", (base["id"],))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (base["id"],))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE reconcile_batches SET sealed_at='x' WHERE id=?", (base["id"],))
    finally:
        conn.close()
    assert client.get(f"/api/reconcile/batches/{base['id']}/verify").json()["intact"] is True


def test_correction_cannot_seal_when_baseline_fingerprint_drifted(client):
    base = _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    assert client.post(f"/api/reconcile/corrections/{corr['id']}/materialize").status_code == 200

    # simulate external damage outside the trigger-guarded path
    conn = connect()
    try:
        conn.execute("DROP TRIGGER trg_match_rows_sealed_upd")
        conn.execute("UPDATE match_rows SET receive_qty=999 WHERE batch_id=?", (base["id"],))
    finally:
        conn.close()

    verify = client.get(f"/api/reconcile/batches/{base['id']}/verify").json()
    assert verify["intact"] is False

    r = client.post(f"/api/reconcile/corrections/{corr['id']}/seal")
    assert r.status_code == 400
    assert "指纹" in r.json()["detail"]

    r = client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    assert r.status_code == 400


def test_fates_are_frozen_on_sealed_correction(client):
    _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    _dispose(client, corr["id"], fate="quarantine")
    vars_ = client.get(f"/api/reconcile/variances?batch_id={corr['id']}").json()
    assert vars_
    assert client.post(f"/api/reconcile/corrections/{corr['id']}/seal").status_code == 200
    r = client.post(f"/api/reconcile/variances/{vars_[0]['id']}/fate", json={"fate": "accept_over"})
    assert r.status_code == 400


def test_new_correction_invalidates_materialisation(client):
    _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    detail = client.get(f"/api/reconcile/batches/{corr['id']}").json()
    assert detail["materialized_at"]

    _correct(client, corr["id"], "CR-2", "SKU-油-5", "confirm", 21)
    detail = client.get(f"/api/reconcile/batches/{corr['id']}").json()
    assert detail["materialized_at"] is None
    r = client.post(f"/api/reconcile/corrections/{corr['id']}/seal")
    assert r.status_code == 400
    assert "物化" in r.json()["detail"]


def test_correction_prev_qty_chains(client):
    _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 39)
    _correct(client, corr["id"], "CR-2", "SKU-米-25", "receive", 40)
    lines = client.get(f"/api/reconcile/corrections?batch_id={corr['id']}").json()
    assert [l["prev_qty"] for l in lines] == [38, 39]
    assert [l["delta"] for l in lines] == [1, 1]


def test_new_key_only_in_correction_batch(client):
    _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-新-1", "expect", 5, supplier="SUP-B")
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    rows = client.get(f"/api/reconcile/matches?batch_id={corr['id']}").json()
    new_row = [m for m in rows if m["sku"] == "SKU-新-1"][0]
    assert new_row["expect_qty"] == 5 and new_row["receive_qty"] == 0
    vars_ = client.get(f"/api/reconcile/variances?batch_id={corr['id']}").json()
    assert [v["bucket"] for v in vars_ if v["sku"] == "SKU-新-1"] == ["new_key"]


def test_open_correction_requires_sealed_day(client):
    r = client.post("/api/reconcile/corrections/open", json={"dock_day": "2026-11-17"})
    assert r.status_code == 400
    assert "尚未结案" in r.json()["detail"]


def test_correction_and_day_gate_do_not_half_write(client):
    base = _close_baseline_day(client)
    results = {}

    def correct_worker():
        conn = connect()
        try:
            from app.services import reconcile_service as svc

            batch = svc.open_correction_batch(conn, DAY)
            results["corr"] = svc.add_correction_line(
                conn,
                batch["id"],
                correction_no="CR-CONC",
                supplier_code="SUP-A",
                sku="SKU-米-25",
                leg="receive",
                new_qty=40,
            )
        except Exception as exc:  # pragma: no cover - diagnostics
            results["corr_err"] = repr(exc)
        finally:
            conn.close()

    def baseline_write_worker():
        conn = connect()
        try:
            from app.services.gate import assert_writable
            from app.db import write_tx

            with write_tx(conn):
                time.sleep(0.05)
                assert_writable(conn, DAY)
                conn.execute(
                    """INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at)
                       VALUES ('EX-CONC','SUP-A','SKU-米-25',?,1,NULL,'2026-11-18T23:00:00Z')""",
                    (DAY,),
                )
        except Exception as exc:
            results["gate_err"] = str(exc)
        finally:
            conn.close()

    t1 = threading.Thread(target=correct_worker)
    t2 = threading.Thread(target=baseline_write_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert "corr" in results, results
    assert "gate_err" in results and "已结案" in results["gate_err"]
    verify = client.get(f"/api/reconcile/batches/{base['id']}/verify").json()
    assert verify["intact"] is True
    detail = client.get(f"/api/reconcile/batches/{results['corr']['batch_id']}").json()
    assert detail["correction_count"] == 1


def test_second_correction_starts_from_first_sealed_correction(client):
    _close_baseline_day(client)
    first = _open_correction(client)
    _correct(client, first["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{first['id']}/materialize")
    _dispose(client, first["id"])
    assert client.post(f"/api/reconcile/corrections/{first['id']}/seal").status_code == 200

    second = _open_correction(client)
    second = _correct(client, second["id"], "CR-2", "SKU-米-25", "receive", 39).json()
    assert second["prev_qty"] == 40
    assert second["delta"] == -1

    # a fresh correction batch still refuses to touch the sealed baseline
    before = client.get("/api/reconcile/batches/1/verify").json()
    client.post(f"/api/reconcile/corrections/{second['batch_id']}/materialize")
    after = client.get("/api/reconcile/batches/1/verify").json()
    assert after["fingerprint"] == before["fingerprint"]


def test_seal_baseline_refuses_stale_materialisation(client):
    client.post("/api/reconcile/run", json={"dock_day": DAY})
    batch_id = client.get(f"/api/reconcile/batches?dock_day={DAY}").json()[0]["id"]
    _dispose(client, batch_id)
    # three-leg edit after materialisation but before the seal
    r = client.post(
        "/api/confirms",
        json={"confirm_no": "CF-9", "supplier_code": "SUP-A", "sku": "SKU-米-25",
              "dock_day": DAY, "qty": 1},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/reconcile/seal", json={"dock_day": DAY})
    assert r.status_code == 400
    assert "重跑对账" in r.json()["detail"]


def test_baseline_detail_lists_reverse_links(client):
    base = _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    _dispose(client, corr["id"])
    client.post(f"/api/reconcile/corrections/{corr['id']}/seal")

    detail = client.get(f"/api/reconcile/batches/{base['id']}").json()
    assert [c["batch_no"] for c in detail["related_corrections"]] == [corr["batch_no"]]
    assert detail["related_corrections"][0]["status"] == "sealed"


def test_correction_fingerprint_covers_correction_lines(client):
    _close_baseline_day(client)
    corr = _open_correction(client)
    _correct(client, corr["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{corr['id']}/materialize")
    _dispose(client, corr["id"])
    client.post(f"/api/reconcile/corrections/{corr['id']}/seal")
    sealed = client.get(f"/api/reconcile/batches/{corr['id']}/verify").json()
    assert sealed["intact"] is True

    conn = connect()
    try:
        conn.execute("DROP TRIGGER trg_correction_lines_sealed_upd")
        conn.execute("UPDATE correction_lines SET new_qty=77 WHERE batch_id=?", (corr["id"],))
    finally:
        conn.close()
    assert client.get(f"/api/reconcile/batches/{corr['id']}/verify").json()["intact"] is False


def test_concurrent_open_reuses_one_correction_batch(client):
    _close_baseline_day(client)
    from app.services import reconcile_service as svc

    ids: list[int] = []
    lock = threading.Lock()

    def worker():
        conn = connect()
        try:
            batch = svc.open_correction_batch(conn, DAY)
            with lock:
                ids.append(batch["id"])
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1, ids


def test_second_correction_materialises_from_first_sealed_state(client):
    _close_baseline_day(client)
    first = _open_correction(client)
    _correct(client, first["id"], "CR-1", "SKU-米-25", "receive", 40)
    client.post(f"/api/reconcile/corrections/{first['id']}/materialize")
    _dispose(client, first["id"])
    client.post(f"/api/reconcile/corrections/{first['id']}/seal")

    second = _open_correction(client)
    _correct(client, second["id"], "CR-2", "SKU-米-25", "receive", 39)
    client.post(f"/api/reconcile/corrections/{second['id']}/materialize")
    rows = client.get(f"/api/reconcile/matches?batch_id={second['id']}").json()
    rice = [m for m in rows if m["sku"] == "SKU-米-25"][0]
    assert rice["receive_qty"] == 39  # not 41: earlier correction is not double-counted
    assert rice["expect_qty"] == 40
