"""采样批次入库与超标裁定。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import SampleIn
from validators import (
    adjudicate_exceed,
    metric_summary,
    validate_flow_cap,
    validate_metrics_nonneg,
    validate_outfall_open,
    validate_plant_running,
)

router = APIRouter(tags=["samples"])

def _next_sample_code(conn, outfall_id: int) -> str:
    outfall = conn.execute(
        "SELECT outfall_code FROM outfalls WHERE id=?", (outfall_id,)
    ).fetchone()
    prefix = outfall["outfall_code"] if outfall else f"OF-{outfall_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM sample_batches WHERE outfall_id=?", (outfall_id,)
    ).fetchone()["c"]
    return f"SM-{prefix}-{n + 1:03d}"


@router.get("/api/samples")
def list_samples(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT s.*, o.outfall_code, p.plant_code FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id"""
        if status:
            rows = conn.execute(
                q + " WHERE s.status=? ORDER BY s.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY s.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics"] or "{}")
            items.append(d)
        return {"items": items}


@router.get("/api/samples/{sample_id}")
def get_sample(sample_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, o.outfall_code, o.cod_limit, o.nh3n_limit, o.tp_limit,
                      p.name plant_name
               FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id
               WHERE s.id=?""",
            (sample_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "采样批次不存在")
        data = dict(row)
        data["metrics"] = json.loads(data["metrics"] or "{}")
        holds = conn.execute(
            "SELECT * FROM exceed_holds WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM discharge_seals WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        return {
            "sample": data,
            "exceed_holds": [dict(h) for h in holds],
            "discharge_seals": [dict(s) for s in seals],
            "discharge_releases": [dict(r) for r in releases],
        }


@router.post("/api/samples")
def create_sample(body: SampleIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"outfall:{body.outfall_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            outfall = conn.execute(
                """SELECT o.*, p.status plant_status, p.design_cap_m3d
                   FROM outfalls o JOIN plants p ON p.id=o.plant_id WHERE o.id=?""",
                (body.outfall_id,),
            ).fetchone()
            if not outfall:
                raise HTTPException(404, "排放口不存在")
            validate_outfall_open(outfall["status"])
            validate_plant_running(outfall["plant_status"])
            metrics = body.metrics.model_dump()
            validate_metrics_nonneg(metrics)
            validate_flow_cap(body.flow_m3h, float(outfall["design_cap_m3d"]))
            permit = conn.execute(
                "SELECT * FROM discharge_permits WHERE permit_code=?",
                (body.permit_ref,),
            ).fetchone()
            if not permit:
                raise HTTPException(400, "排放许可不存在")
            if permit["status"] != "有效":
                raise HTTPException(400, "排放许可未生效")
            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics,
                {
                    "cod": outfall["cod_limit"],
                    "nh3n": outfall["nh3n_limit"],
                    "tp": outfall["tp_limit"],
                },
            )
            code = body.sample_code or _next_sample_code(conn, body.outfall_id)
            if conn.execute(
                "SELECT id FROM sample_batches WHERE sample_code=?", (code,)
            ).fetchone():
                raise HTTPException(400, "采样编号已存在")
            status = "旁路挂起" if exceeded else "已裁定"
            adjudicated = None if exceeded else now_iso()
            cur = conn.execute(
                """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
                   flow_m3h,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.outfall_id,
                    body.permit_ref,
                    json.dumps(metrics, ensure_ascii=False),
                    body.flow_m3h,
                    1,
                    status,
                    adjudicated,
                    now_iso(),
                ),
            )
            sid = cur.lastrowid
            hold = None
            if exceeded:
                hold_key = f"EX-{code}-{metric}"
                conn.execute(
                    """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                       limit_value,status,opened_at,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        hold_key,
                        metric,
                        measured,
                        limit,
                        "开放",
                        now_iso(),
                        "入库自动挂起",
                    ),
                )
                hold = hold_key
            write_audit(
                conn,
                user,
                "sample_create",
                code,
                {
                    "status": status,
                    "metrics": metrics,
                    "summary": metric_summary(metrics),
                    "exceed": exceeded,
                },
            )
            conn.commit()
            return {
                "id": sid,
                "sample_code": code,
                "status": status,
                "exceed": exceeded,
                "hold_key": hold,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


def apply_rejudge_metrics(conn, sample, new_metrics: dict) -> dict:
    """BUGFIX-06 甲路径：在调用方事务内改写正式指标并同事务重裁定。

    必须与空桩回执、对账刷新、血缘改判边处于同一事务；本函数不自行 commit。
    - 正式指标三项整体覆盖，generation 升代，记录新的 adjudicated_at；
    - 按排放口限值重裁定：新值超标则重开/保留开放挂起，否则关闭全部挂起并回到已裁定。
    """
    sid = sample["id"]
    outfall = conn.execute(
        "SELECT cod_limit, nh3n_limit, tp_limit FROM outfalls WHERE id=?",
        (sample["outfall_id"],),
    ).fetchone()
    validate_metrics_nonneg(new_metrics)
    exceeded, metric, measured, limit = adjudicate_exceed(
        new_metrics,
        {
            "cod": outfall["cod_limit"],
            "nh3n": outfall["nh3n_limit"],
            "tp": outfall["tp_limit"],
        },
    )
    new_generation = int(sample["generation"]) + 1
    if exceeded:
        new_status = "旁路挂起"
        adjudicated_at = None
    else:
        new_status = "已裁定"
        adjudicated_at = now_iso()
    conn.execute(
        """UPDATE sample_batches
           SET metrics=?, generation=?, status=?, adjudicated_at=?
           WHERE id=?""",
        (
            json.dumps(new_metrics, ensure_ascii=False),
            new_generation,
            new_status,
            adjudicated_at,
            sid,
        ),
    )
    # 重裁定超标页：先闭环旧挂起，新值超标再开一条改判挂起，保持五页同源
    conn.execute(
        """UPDATE exceed_holds SET status='已关闭', closed_at=COALESCE(closed_at,?),
           note=note || ' | 外送改判重裁定'
           WHERE sample_id=? AND status='开放'""",
        (now_iso(), sid),
    )
    hold_key = None
    if exceeded:
        hold_key = f"EX-{sample['sample_code']}-{metric}-RJ{new_generation}"
        conn.execute(
            """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
               limit_value,status,opened_at,note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                sid,
                hold_key,
                metric,
                measured,
                limit,
                "开放",
                now_iso(),
                "外送改判重裁定挂起",
            ),
        )
    return {
        "sample_id": sid,
        "sample_code": sample["sample_code"],
        "metrics": new_metrics,
        "generation": new_generation,
        "status": new_status,
        "exceeded": exceeded,
        "hold_key": hold_key,
        "summary": metric_summary(new_metrics),
    }
