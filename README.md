# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印；结案后发现错数走**挂靠原日的只读订正批**。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```

```bash
cd backend && python -m pytest tests -q   # 后端契约测试
```

## 订正架构（挂靠原日只读订正批）

结案后原批封印，三腿与原批明细都是只读的。订正**不回写原批**，而是：

| 环节 | 行为 |
| --- | --- |
| 订正入口 | `POST /api/reconcile/corrections/open`，仅当该日已有 sealed 基线批；同日未结案的订正批会被复用 |
| 原批只读闸 | `gate.assert_writable` 挡三腿写入；`match_rows / variance_rows / correction_lines / reconcile_batches` 上的触发器再兜底一次：sealed 批明细一律 `RAISE(ABORT)` |
| 订正行入湖 | `correction_lines`（`correction_no / leg / prev_qty / new_qty / delta / reason`），只挂到 `kind='correction'` 的批次，写原批直接被拒 |
| 对齐物化 | `POST /api/reconcile/corrections/{id}/materialize`：原批行 ± 订正 delta 重新物化成订正批自己的 `match_rows / variance_rows`；新增任何订正行都会作废上一次物化 |
| 差额分桶 | `bucket`：`leg_low / leg_high / mixed / new_key`；未改动且原批已处置的行按 `origin='carried'` 沿用原命运 |
| 结案写闸 | 必须先物化、必须有订正行、未决差额清零；且原封印指纹必须仍与现行一致，否则拒绝（订正成功却无法对照原封印） |
| 批次快照 | `batch_snapshots` 记录 opened / materialized / sealed 三阶段 SHA-256 指纹（覆盖批次头、对齐行、差额行、订正行） |
| 总览现行 | `/api/dashboard` 同时返回 `baseline_batch` 与 `correction_batch`，各自带 `intact`；对账页按批次切换，`corrects_batch_no` 与 `related_corrections` 双向可回溯 |

并发：三腿写入、跑对账、结案、写订正都用 `BEGIN IMMEDIATE` 串行化（`app.db.write_tx`），只读闸和它保护的写在同一临界区里，不存在「回执成功但原 sealed 已脏」的半截状态。

口径：**原 sealed 日的三腿与批永远只读；订正落在挂靠原日的独立订正批；原批数量永远等于封印时的数量。**
