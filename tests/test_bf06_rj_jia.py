# -*- coding: utf-8 -*-
"""effluent-bugfix-06 红测甲（rj=改判/jia=甲）：
改判回执已回，但采样正式指标仍旧值——写口只更新空桩回执，未同事务改指标。

修复口径（污水侧甲路径，当班封死）：
改判须在同一事务内改写 sample_batches 正式指标、重裁定超标页、登记血缘改判边，
并让对账按新指标刷新；任一环节失败整笔回退。禁止只改空桩不改指标。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "bf06_jia.db")
    init_db()
    return TestClient(app)


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post("/api/login", json={"username": user, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _seed_pending_sample(c: TestClient, h: dict) -> dict:
    """入库一笔 COD 超标（58>50）的旁路挂起批次，作为外送化验改判对象。"""
    outfalls = c.get("/api/outfalls", headers=h).json()["items"]
    of = next(x for x in outfalls if x["outfall_code"] == "OF-E1-A")
    created = c.post(
        "/api/samples",
        headers=h,
        json={
            "outfall_id": of["id"],
            "permit_ref": "DP-2026-E01",
            "sample_code": "SM-BF06-JIA-001",
            "flow_m3h": 2800,
            "metrics": {"cod": 58.0, "nh3n": 4.1, "tp": 0.33},
        },
    ).json()
    assert created["exceed"] is True
    return created


def _enqueue_lab_job(c: TestClient, h: dict, sample_code: str) -> int:
    job = c.post(
        "/api/stubs/lab-courier",
        headers=h,
        json={"sample_code": sample_code, "courier_slot": "A21", "mode": "enqueue"},
    ).json()
    return job["id"]


def test_bf06_jia_rejudge_must_rewrite_official_metrics(client):
    """甲：改判后采样正式指标必须与改判新值一致（当前实现只改空桩回执，必红）。"""
    c = client
    h = _auth(c)
    sample = _seed_pending_sample(c, h)
    job_id = _enqueue_lab_job(c, h, "SM-BF06-JIA-001")
    new_metrics = {"cod": 44.0, "nh3n": 3.6, "tp": 0.29}

    resp = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"detail": "外送化验改判：复检合格", "metrics": new_metrics},
    )
    assert resp.status_code == 200, resp.text

    detail = c.get(f"/api/samples/{sample['id']}", headers=h).json()
    official = detail["sample"]["metrics"]
    assert official == new_metrics, (
        f"改判后正式指标仍旧值：{official}，未与空桩改判同事务双写"
    )
    # 回执必须声明指标已同步，禁止 metrics_synced=False 的假回执
    assert resp.json().get("metrics_synced") is True


def test_bf06_jia_rejudge_without_metrics_is_rejected(client):
    """偏航拒写：只带文案不带正式指标的改判请求必须被拒，禁止只改空桩页文案。"""
    c = client
    h = _auth(c)
    sample = _seed_pending_sample(c, h)
    old_metrics = c.get(f"/api/samples/{sample['id']}", headers=h).json()["sample"]["metrics"]
    job_id = _enqueue_lab_job(c, h, "SM-BF06-JIA-001")

    resp = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"detail": "只改文案不带指标"},
    )
    assert resp.status_code == 400, resp.text

    job = next(j for j in c.get("/api/stubs/lab-courier", headers=h).json()["items"] if j["id"] == job_id)
    assert job["status"] != "已改判", "偏航请求不得把空桩回执改成已改判"
    still = c.get(f"/api/samples/{sample['id']}", headers=h).json()["sample"]["metrics"]
    assert still == old_metrics


def test_bf06_jia_rejudge_rolls_back_together_on_failure(client, monkeypatch):
    """同事务原子性：指标写入后若后续环节失败，空桩回执也必须回退，不得留下分裂态。"""
    c = client
    h = _auth(c)
    sample = _seed_pending_sample(c, h)
    job_id = _enqueue_lab_job(c, h, "SM-BF06-JIA-001")
    new_metrics = {"cod": 44.0, "nh3n": 3.6, "tp": 0.29}

    import metric_lineage_svc

    def _boom(*args, **kwargs):
        raise RuntimeError("lineage write failure injected")

    # record_rejudge_edge 由本次修复引入，红阶段尚不存在，raising=False 保证
    # 红阶段先命中业务断言；修复后写口调用该函数即触发注入失败
    monkeypatch.setattr(metric_lineage_svc, "record_rejudge_edge", _boom, raising=False)

    with pytest.raises(Exception):
        c.post(
            f"/api/stubs/lab-courier/{job_id}/rejudge",
            headers=h,
            json={"detail": "注入失败", "metrics": new_metrics},
        )

    job = next(j for j in c.get("/api/stubs/lab-courier", headers=h).json()["items"] if j["id"] == job_id)
    assert job["status"] != "已改判", "事务失败时空桩回执必须随指标一并回退"
    metrics_row = c.get(f"/api/samples/{sample['id']}", headers=h).json()["sample"]["metrics"]
    assert metrics_row == {"cod": 58.0, "nh3n": 4.1, "tp": 0.33}


def test_bf06_jia_business_http_error_rolls_back_entire_tx(client):
    """业务段 400（负指标）必须整笔回滚：不得部分提交，不留锁、回执、血缘、审计。"""
    c = client
    h = _auth(c)
    sample = _seed_pending_sample(c, h)
    job_id = _enqueue_lab_job(c, h, "SM-BF06-JIA-001")

    resp = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"metrics": {"cod": -1.0, "nh3n": 3.6, "tp": 0.29}},
    )
    assert resp.status_code == 400, resp.text

    from db import connect

    with connect() as conn:
        job = conn.execute(
            "SELECT status FROM lab_courier_jobs WHERE id=?", (job_id,)
        ).fetchone()
        metrics = json.loads(
            conn.execute(
                "SELECT metrics FROM sample_batches WHERE id=?", (sample["id"],)
            ).fetchone()["metrics"]
        )
        locks = conn.execute("SELECT COUNT(*) c FROM effluent_locks").fetchone()["c"]
        edges = conn.execute(
            "SELECT COUNT(*) c FROM metric_lineage WHERE edge_type='外送改判'"
        ).fetchone()["c"]
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='lab_rejudge'"
        ).fetchone()["c"]
    assert job["status"] != "已改判"
    assert metrics == {"cod": 58.0, "nh3n": 4.1, "tp": 0.33}
    assert locks == 0, "业务回滚须一并撤销采样锁，无残留"
    assert edges == 0 and audits == 0, "业务回滚不得留下血缘边或双写审计"


def test_bf06_jia_metrics_persisted_as_json_triplet(client):
    """夹具复现口径：改判后库里存的正式指标三项齐全且可被采样页 JSON 解析。"""
    c = client
    h = _auth(c)
    sample = _seed_pending_sample(c, h)
    job_id = _enqueue_lab_job(c, h, "SM-BF06-JIA-001")
    new_metrics = {"cod": 41.5, "nh3n": 3.1, "tp": 0.21}

    r = c.post(
        f"/api/stubs/lab-courier/{job_id}/rejudge",
        headers=h,
        json={"metrics": new_metrics},
    )
    assert r.status_code == 200
    from db import connect

    with connect() as conn:
        raw = conn.execute(
            "SELECT metrics FROM sample_batches WHERE id=?", (sample["id"],)
        ).fetchone()["metrics"]
    assert json.loads(raw) == new_metrics
