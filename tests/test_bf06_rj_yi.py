# -*- coding: utf-8 -*-
"""effluent-bugfix-06 红测乙（rj=改判/yi=乙）：
空桩页已改判，而许可对账仍按旧指标核算——对账与空桩分裂。

修复口径：改判同事务刷新正式指标后，对账页必须按同一份新指标重算，
空桩页/采样页/对账页/血缘页/超标页五页同源；血缘页须可追溯到改判边。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

PERMIT = "DP-2026-E01"
SAMPLE_CODE = "SM-BF06-YI-001"
OLD_METRICS = {"cod": 58.0, "nh3n": 4.1, "tp": 0.33}
# 改判新值全部落在排放口限值（50/5/0.5）与许可帽之内
NEW_METRICS = {"cod": 42.0, "nh3n": 3.2, "tp": 0.28}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "bf06_yi.db")
    init_db()
    return TestClient(app)


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _rejudge_ready(c: TestClient, h: dict) -> int:
    outfalls = c.get("/api/outfalls", headers=h).json()["items"]
    of = next(x for x in outfalls if x["outfall_code"] == "OF-E1-A")
    created = c.post(
        "/api/samples",
        headers=h,
        json={
            "outfall_id": of["id"],
            "permit_ref": PERMIT,
            "sample_code": SAMPLE_CODE,
            "flow_m3h": 2800,
            "metrics": OLD_METRICS,
        },
    ).json()
    assert created["status"] == "旁路挂起"
    job = c.post(
        "/api/stubs/lab-courier",
        headers=h,
        json={"sample_code": SAMPLE_CODE, "courier_slot": "A21", "mode": "enqueue"},
    ).json()
    return job["id"]


def test_bf06_yi_reconcile_refreshes_to_rejudged_metrics(client):
    """乙：改判后对账页该采样必须按新指标核算，不得保留旧值。"""
    c = client
    h = _auth(c)
    job_id = _rejudge_ready(c, h)

    r = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"detail": "复检合格改判", "metrics": NEW_METRICS},
    )
    assert r.status_code == 200, r.text

    recon = c.get(f"/api/reconcile/{PERMIT}", headers=h).json()
    row = next(s for s in recon["samples"] if s["sample_code"] == SAMPLE_CODE)
    assert row["metrics"] == NEW_METRICS, (
        f"对账仍旧值：{row['metrics']}，空桩已改判而对账分裂"
    )
    assert row["matched"] is True


def test_bf06_yi_five_pages_same_source_after_rejudge(client):
    """五页同源：空桩/采样/对账取同一份新指标，超标页随改判重裁定，血缘页留改判边。"""
    c = client
    h = _auth(c)
    job_id = _rejudge_ready(c, h)

    # 改判前：超标页有一条开放挂起
    holds_before = [
        x for x in c.get("/api/exceeds?status=开放", headers=h).json()["items"]
        if x["sample_code"] == SAMPLE_CODE
    ]
    assert len(holds_before) == 1

    r = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"metrics": NEW_METRICS},
    )
    assert r.status_code == 200

    # 空桩页
    job = next(j for j in c.get("/api/stubs/lab-courier", headers=h).json()["items"] if j["id"] == job_id)
    assert job["status"] == "已改判"
    # 采样页
    sample = next(s for s in c.get("/api/samples", headers=h).json()["items"] if s["sample_code"] == SAMPLE_CODE)
    assert sample["metrics"] == NEW_METRICS
    # 对账页
    recon = c.get(f"/api/reconcile/{PERMIT}", headers=h).json()
    recon_row = next(s for s in recon["samples"] if s["sample_code"] == SAMPLE_CODE)
    assert recon_row["metrics"] == NEW_METRICS
    # 超标页：新值不超标，开放挂起须闭环，采样状态回到已裁定
    open_holds = [
        x for x in c.get("/api/exceeds?status=开放", headers=h).json()["items"]
        if x["sample_code"] == SAMPLE_CODE
    ]
    assert open_holds == []
    assert sample["status"] == "已裁定"
    # 血缘页：可追溯改判边
    edges = c.get("/api/lineage", headers=h).json()["items"]
    rejudge_edges = [
        e for e in edges
        if e["from_code"] == SAMPLE_CODE and e["edge_type"] == "外送改判"
    ]
    assert len(rejudge_edges) == 1
    assert SAMPLE_CODE in rejudge_edges[0]["detail"]


def test_bf06_yi_rejudge_to_worse_metrics_reopens_exceed(client):
    """反向同源：改判后若新值超标，超标页须重开挂起，三页指标仍为同一新值。"""
    c = client
    h = _auth(c)
    job_id = _rejudge_ready(c, h)
    worse = {"cod": 63.0, "nh3n": 4.6, "tp": 0.4}

    r = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"detail": "复检确认超标", "metrics": worse},
    )
    assert r.status_code == 200

    sample = next(s for s in c.get("/api/samples", headers=h).json()["items"] if s["sample_code"] == SAMPLE_CODE)
    assert sample["metrics"] == worse
    assert sample["status"] == "旁路挂起"
    open_holds = [
        x for x in c.get("/api/exceeds?status=开放", headers=h).json()["items"]
        if x["sample_code"] == SAMPLE_CODE
    ]
    assert len(open_holds) == 1
    assert open_holds[0]["measured"] == 63.0
    recon = c.get(f"/api/reconcile/{PERMIT}", headers=h).json()
    recon_row = next(s for s in recon["samples"] if s["sample_code"] == SAMPLE_CODE)
    assert recon_row["metrics"] == worse


def test_bf06_yi_audit_trail_marks_double_write(client):
    """审计口径：改判审计须标记指标已同事务同步，不得再留 metrics_synced=False。"""
    c = client
    h = _auth(c)
    job_id = _rejudge_ready(c, h)

    r = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"metrics": NEW_METRICS},
    )
    assert r.status_code == 200

    audit = c.get("/api/audit", headers=h).json()["items"]
    entries = [a for a in audit if a["action"] == "lab_rejudge"]
    assert entries, "缺少 lab_rejudge 审计轨迹"
    import json

    payload = json.loads(entries[0]["payload"])
    assert payload.get("metrics_synced") is True
