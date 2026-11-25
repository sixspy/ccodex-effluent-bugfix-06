# -*- coding: utf-8 -*-
"""effluent-bugfix-06 红测争（rj=改判/zh=争用）：
两路会话对同一批改判提交，写口须抢 sample:{id} 采样锁，
败方只记 contention_log 争用痕并返回 409、零改；胜方同事务双写并在提交后释放锁。

确定性说明：SQLite 为单写者库，BEGIN IMMEDIATE 已在物理层串行化并发写事务，
真正需要钉住的是应用层采样锁契约——当另一会话持有该采样锁时，竞争会话必须
被 409 拒绝、只留一条"争用失败"痕且不得改任何业务数据。本测试用两个独立鉴权
会话、一个持久持锁方来确定性复现该契约，避免线程调度造成的时序 flake。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

SAMPLE_CODE = "SM-BF06-ZH-001"
OLD_METRICS = {"cod": 58.0, "nh3n": 4.1, "tp": 0.33}
WIN_METRICS = {"cod": 42.0, "nh3n": 3.2, "tp": 0.28}
LOSE_METRICS = {"cod": 47.5, "nh3n": 3.9, "tp": 0.31}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "bf06_zh.db")
    init_db()
    return TestClient(app)


def _auth(c: TestClient, user: str, password: str) -> dict:
    tok = c.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _prepare(client: TestClient, h: dict) -> tuple[int, int]:
    outfalls = client.get("/api/outfalls", headers=h).json()["items"]
    of = next(x for x in outfalls if x["outfall_code"] == "OF-E1-A")
    created = client.post(
        "/api/samples",
        headers=h,
        json={
            "outfall_id": of["id"],
            "permit_ref": "DP-2026-E01",
            "sample_code": SAMPLE_CODE,
            "flow_m3h": 2800,
            "metrics": OLD_METRICS,
        },
    ).json()
    job = client.post(
        "/api/stubs/lab-courier",
        headers=h,
        json={"sample_code": SAMPLE_CODE, "courier_slot": "A21", "mode": "enqueue"},
    ).json()
    return created["id"], job["id"]


def test_bf06_zh_dual_session_rejudge_requires_sample_lock(client):
    """两个会话争同一采样：持锁会话为准，竞争会话 409、只留争用失败痕、零改。"""
    c = client
    h_holder = _auth(c, "writer", "writer123")   # 持锁会话（在途改判方）
    h_rival = _auth(c, "admin", "admin123")      # 竞争会话（败方）
    sample_id, job_id = _prepare(c, h_holder)
    lock_key = f"sample:{sample_id}"

    # 持锁会话先抢到采样锁（模拟其改判事务正在进行，锁行已提交可见）
    acq = c.post(f"/api/locks/acquire?lock_key={lock_key}", headers=h_holder)
    assert acq.status_code == 200

    # 竞争会话打同一批改判写口：必须被采样锁 409 拒绝
    rival_resp = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h_rival,
        json={"detail": "admin 竞争改判", "metrics": LOSE_METRICS},
    )
    assert rival_resp.status_code == 409, rival_resp.text

    with connect() as conn:
        final_metrics = json.loads(
            conn.execute(
                "SELECT metrics FROM sample_batches WHERE id=?", (sample_id,)
            ).fetchone()["metrics"]
        )
        sample_status = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (sample_id,)
        ).fetchone()["status"]
        contention = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log WHERE action='争用失败'"
            ).fetchall()
        ]
        job_status = conn.execute(
            "SELECT status FROM lab_courier_jobs WHERE id=?", (job_id,)
        ).fetchone()["status"]
        edges = conn.execute(
            "SELECT COUNT(*) c FROM metric_lineage WHERE edge_type='外送改判'"
        ).fetchone()["c"]

    # 败方零改：正式指标、采样状态、空桩回执、血缘全部不动
    assert final_metrics == OLD_METRICS, "败方不得改写正式指标"
    assert sample_status == "旁路挂起"
    assert job_status != "已改判", "败方不得把空桩回执改成已改判"
    assert edges == 0, "败方不得登记改判血缘边"
    # 只留一条争用失败痕，锚定到该采样锁与败方
    assert len(contention) == 1, "败方必须只记一条争用失败痕"
    assert contention[0]["lock_key"] == lock_key
    assert contention[0]["actor"] == "admin"

    # 持锁会话随后正常改判成功（同 holder 可重入），完成后释放锁
    win_resp = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h_holder,
        json={"detail": "writer 持锁改判", "metrics": WIN_METRICS},
    )
    assert win_resp.status_code == 200, win_resp.text
    rel = c.post(f"/api/locks/release?lock_key={lock_key}", headers=h_holder)
    assert rel.status_code == 200

    with connect() as conn:
        final = json.loads(
            conn.execute(
                "SELECT metrics FROM sample_batches WHERE id=?", (sample_id,)
            ).fetchone()["metrics"]
        )
        locks_left = conn.execute(
            "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
        ).fetchall()
        traces = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE action='争用失败'"
        ).fetchone()["c"]
    assert final == WIN_METRICS, "胜方改判后正式指标应为胜方值"
    assert len(locks_left) == 0, "收尾后采样锁须释放，无残留"
    assert traces == 1, "胜方成功不追加争用痕，仍只一条"


def test_bf06_zh_lock_released_after_winner_commits(client):
    """无外部持锁时：改判 200 同事务双写，提交后采样锁自动释放，不留死锁。"""
    c = client
    h = _auth(c, "admin", "admin123")
    sample_id, job_id = _prepare(c, h)

    r1 = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"metrics": WIN_METRICS},
    )
    assert r1.status_code == 200

    with connect() as conn:
        left = conn.execute(
            "SELECT holder FROM effluent_locks WHERE lock_key=?",
            (f"sample:{sample_id}",),
        ).fetchone()
    assert left is None, "改判提交后采样锁未释放"
