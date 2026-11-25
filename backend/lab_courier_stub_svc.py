"""空桩：外送化验快递槽（题面可点到接口，默认 dry_run）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import acquire_sample_lock_tx, release
import metric_lineage_svc
from metrics_math import METRIC_KEYS
from models import LabCourierIn
import reconcile_svc
import samples_svc

router = APIRouter(tags=["lab_courier_stub"])


def _log_contention(lock_key: str, actor: str, reason: str, action: str = "改判偏航拒写") -> None:
    """用独立短事务落一条争用/偏航痕，与主写事务隔离，保证留痕不被回滚。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, actor, action, reason, now_iso()),
        )
        conn.commit()


@router.get("/api/stubs/lab-courier")
def list_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lab_courier_jobs ORDER BY id DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "stub": True, "ready": False}


@router.post("/api/stubs/lab-courier")
def enqueue(body: LabCourierIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    if body.mode not in ("dry_run", "enqueue"):
        raise HTTPException(400, "仅支持 dry_run / enqueue")
    with connect() as conn:
        status = "演练" if body.mode == "dry_run" else "排队"
        cur = conn.execute(
            """INSERT INTO lab_courier_jobs(sample_code,courier_slot,mode,status,detail,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                body.sample_code,
                body.courier_slot,
                body.mode,
                status,
                body.detail or "空桩未接通外送",
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lab_courier_stub",
            body.sample_code,
            body.model_dump(),
        )
        conn.commit()
        return {"id": cur.lastrowid, "status": status, "stub": True}


@router.post("/api/stubs/lab-courier/{job_id}/rejudge")
def rejudge(job_id: int, body: dict, authorization: AuthHeader = None):
    """BUGFIX-06 修复：改判同事务双写。

    污水侧甲路径（当班封死）：空桩回执、采样正式指标、超标重裁定、对账刷新、
    血缘改判边必须在同一 BEGIN IMMEDIATE 事务内完成，任一失败整笔回退。
    写口先抢 sample:{id} 采样锁；偏航请求（缺指标/缺采样）拒写，禁止只改空桩文案。
    """
    user = require_writer(authorization)
    metrics_in = (body or {}).get("metrics")
    if not isinstance(metrics_in, dict) or not metrics_in:
        # 偏航拒写：不允许只改空桩页文案而不动正式指标
        with connect() as audit_conn:
            audit_conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (f"lab-job:{job_id}", user, "改判偏航拒写", "缺正式指标，禁止只改回执", now_iso()),
            )
            audit_conn.commit()
        raise HTTPException(400, "改判必须携带正式指标 metrics，禁止只改空桩回执")
    missing = [k for k in METRIC_KEYS if k not in metrics_in]
    if missing:
        raise HTTPException(400, f"正式指标缺项：{','.join(missing)}")
    new_metrics = {k: float(metrics_in[k]) for k in METRIC_KEYS}

    with connect() as conn:
        # IMMEDIATE：第一时间拿写锁，两路并发改判在此确定性串行
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT * FROM lab_courier_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise HTTPException(404, "空桩任务不存在")
        sample = conn.execute(
            "SELECT * FROM sample_batches WHERE sample_code=?", (job["sample_code"],)
        ).fetchone()
        if job["status"] == "已改判":
            # 偏航留痕：先回滚当前（空）事务，再用独立短事务落痕，避免与主写事务交错
            conn.rollback()
            _log_contention(
                f"lab-job:{job_id}", user, f"空桩任务 {job_id} 已改判，禁止重复改判"
            )
            raise HTTPException(409, f"空桩任务 {job_id} 已改判，禁止重复改判")
        if not sample:
            conn.rollback()
            reason = f"采样 {job['sample_code']} 不存在，改判拒写"
            _log_contention(f"lab-job:{job_id}", user, reason)
            raise HTTPException(409, reason)

        lock_key = f"sample:{sample['id']}"
        try:
            # 锁判定独立成段：此 409 时事务内只有一条争用痕，提交留痕即安全
            acquire_sample_lock_tx(conn, sample["id"], user)
        except HTTPException:
            conn.commit()
            raise

        old_metrics = json.loads(sample["metrics"] or "{}")
        try:
            # 1) 采样服务：正式指标改写 + 升代 + 超标页重裁定（同事务）
            applied = samples_svc.apply_rejudge_metrics(conn, sample, new_metrics)
            # 2) 血缘服务：登记外送改判边（同事务，失败则整笔回退）
            edge_id = metric_lineage_svc.record_rejudge_edge(
                conn, sample["id"], old_metrics, new_metrics
            )
            # 3) 对账服务：按新指标即时刷新（读正式区，无旧值缓存）
            recon = reconcile_svc.refresh_reconcile_view(conn, sample["id"])
            detail = body.get("detail") or "外送化验改判已回"
            # 4) 空桩回执：最后置为已改判，与指标同生共死
            conn.execute(
                "UPDATE lab_courier_jobs SET status=?, detail=? WHERE id=?",
                ("已改判", detail, job_id),
            )
            write_audit(
                conn,
                user,
                "lab_rejudge",
                job["sample_code"],
                {
                    "metrics_synced": True,
                    "old_metrics": old_metrics,
                    "new_metrics": new_metrics,
                    "generation": applied["generation"],
                    "lineage_edge_id": edge_id,
                    "reconcile_matched": recon["matched"],
                    "lock_key": lock_key,
                },
            )
            conn.commit()
        except BaseException:
            # 业务段任何失败（含 HTTPException 400）：整笔回退，不留分裂态
            conn.rollback()
            raise
        else:
            # 仅成功路径释放本连接抢到的采样锁（独立短事务，不触碰主事务结果）
            with connect() as lock_conn:
                release(lock_conn, lock_key, user)
                lock_conn.commit()
        return {
            "ok": True,
            "metrics_synced": True,
            "status": "已改判",
            "sample_id": sample["id"],
            "generation": applied["generation"],
            "sample_status": applied["status"],
            "exceeded": applied["exceeded"],
            "hold_key": applied["hold_key"],
            "lineage_edge_id": edge_id,
            "reconcile": recon,
        }
