# -*- coding: utf-8 -*-
"""duel_rejudge_dual — effluent-bugfix-06 争用侧独立并发脚本。

两个会话对同一批外送化验改判写口争 sample:{id} 采样锁：
  * 持锁会话（writer）模拟在途改判事务，锁行已提交可见；
  * 竞争会话（admin）打同一写口，修复前无采样锁 → 200 双写分裂（红）；
    修复后 → 409 只记一条 contention_log 争用失败痕、业务零改（绿）；
  * 随后持锁会话正常改判成功，提交后释放锁，正式指标只留胜方值。

SQLite 单写者下 BEGIN IMMEDIATE 已在物理层串行写事务，本脚本用持久持锁方
确定性钉住应用层采样锁契约，避免线程调度 flake。

用法（隔离模式运行，DB 指向临时文件，不碰 data/app.db）：
  python -I tests/duel_rejudge_dual.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import connect, init_db  # noqa: E402

SAMPLE_CODE = "SM-BF06-DUEL-001"
OLD_METRICS = {"cod": 58.0, "nh3n": 4.1, "tp": 0.33}
WIN_METRICS = {"cod": 42.0, "nh3n": 3.2, "tp": 0.28}
LOSE_METRICS = {"cod": 47.5, "nh3n": 3.9, "tp": 0.31}


def _auth(c: TestClient, user: str, password: str) -> dict:
    tok = c.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def main() -> int:
    import db

    db.DB_PATH = Path(tempfile.mkdtemp(prefix="bf06_duel_")) / "duel.db"
    init_db()

    holder = TestClient(app)          # 持锁会话（胜方：writer）
    rival = TestClient(app)           # 竞争会话（败方：admin）
    h_holder = _auth(holder, "writer", "writer123")
    h_rival = _auth(rival, "admin", "admin123")

    of = next(x for x in holder.get("/api/outfalls", headers=h_holder).json()["items"]
              if x["outfall_code"] == "OF-E1-A")
    sample = holder.post(
        "/api/samples",
        headers=h_holder,
        json={
            "outfall_id": of["id"],
            "permit_ref": "DP-2026-E01",
            "sample_code": SAMPLE_CODE,
            "flow_m3h": 2800,
            "metrics": OLD_METRICS,
        },
    ).json()
    sample_id = sample["id"]
    job_id = holder.post(
        "/api/stubs/lab-courier",
        headers=h_holder,
        json={"sample_code": SAMPLE_CODE, "courier_slot": "A21", "mode": "enqueue"},
    ).json()["id"]
    lock_key = f"sample:{sample_id}"

    # 持锁会话先占采样锁（在途改判事务）
    holder.post(f"/api/locks/acquire?lock_key={lock_key}", headers=h_holder)

    # 竞争会话打同一写口
    rival_resp = rival.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h_rival,
        json={"detail": "admin 竞争改判", "metrics": LOSE_METRICS},
    )
    print("竞争会话 http =", rival_resp.status_code)

    # 持锁会话随后正常改判并释放
    win_resp = holder.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h_holder,
        json={"detail": "writer 持锁改判", "metrics": WIN_METRICS},
    )
    print("持锁会话 http =", win_resp.status_code)
    holder.post(f"/api/locks/release?lock_key={lock_key}", headers=h_holder)

    with connect() as conn:
        final_metrics = json.loads(conn.execute(
            "SELECT metrics FROM sample_batches WHERE id=?", (sample_id,)
        ).fetchone()["metrics"])
        contention = [dict(r) for r in conn.execute(
            "SELECT * FROM contention_log WHERE action='争用失败'"
        ).fetchall()]
        locks_left = [dict(r) for r in conn.execute(
            "SELECT * FROM effluent_locks WHERE lock_key=?", (lock_key,)
        ).fetchall()]
        edges = conn.execute(
            "SELECT COUNT(*) c FROM metric_lineage WHERE edge_type='外送改判'"
        ).fetchone()["c"]

    print("正式指标终值:", final_metrics)
    print("争用失败痕:", len(contention), contention)
    print("改判血缘边:", edges, "| 残留锁:", locks_left)

    ok = (
        rival_resp.status_code == 409
        and win_resp.status_code == 200
        and final_metrics == WIN_METRICS
        and len(contention) == 1
        and contention[0]["lock_key"] == lock_key
        and locks_left == []
        and edges == 1
    )
    print("判定:", "绿（采样锁串行化，败方零改只留争用痕）" if ok else "红（竞争会话无锁写入，双写分裂）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
