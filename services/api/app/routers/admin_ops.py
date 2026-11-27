"""
Роутер Ops & Cost (Экран 4):
- GET  /v1/admin/ops
- POST /v1/admin/chaos
- GET  /v1/admin/cost
- GET  /v1/admin/qc-report
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.api.app.routers.admin_deps import (
    get_artifacts_dir,
    get_chaos_state,
    get_current_config_version,
    get_db,
    get_index_version,
    require_admin_token,
    set_chaos_state,
)

router = APIRouter(prefix="/v1/admin", tags=["admin-ops"], dependencies=[Depends(require_admin_token)])


class ChaosPatchIn(BaseModel):
    force_disable_llm: Optional[bool] = None
    force_disable_dense: Optional[bool] = None
    force_disable_overlay: Optional[bool] = None
    degradation_level: Optional[str] = None


@router.get("/ops")
def get_ops_status(request: Request) -> Dict[str, Any]:
    chaos = get_chaos_state(request)
    with get_db() as conn:
        cfg_ver = get_current_config_version(conn)

        lat_rows = conn.execute(
            "SELECT latency_ms FROM search_traces ORDER BY ts DESC LIMIT 1000"
        ).fetchall()
        latencies = [float(r["latency_ms"]) for r in lat_rows if r["latency_ms"] is not None]
        p50_ms = round(float(np.percentile(latencies, 50)), 2) if latencies else 0.0
        p95_ms = round(float(np.percentile(latencies, 95)), 2) if latencies else 0.0

        cache_entries = int(conn.execute("SELECT COUNT(*) FROM qu_cache").fetchone()[0])

        # Оценка cache hit и дневного расхода LLM
        today_prefix = conn.execute("SELECT strftime('%Y-%m-%d', 'now')").fetchone()[0]
        spend_row = conn.execute(
            "SELECT COALESCE(SUM(cost_micro_rub), 0) AS s, COUNT(*) AS cnt FROM cost_ledger WHERE ts LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()
        daily_spend = int(spend_row["s"] or 0)
        daily_llm_calls = int(spend_row["cnt"] or 0)

        total_traces = int(conn.execute("SELECT COUNT(*) FROM search_traces").fetchone()[0])
        llm_traces = int(
            conn.execute("SELECT COUNT(DISTINCT trace_id) FROM cost_ledger WHERE purpose = 'qu' AND trace_id IS NOT NULL").fetchone()[0]
        )

    daily_budget = int(os.getenv("LLM_DAILY_BUDGET_MICRO_RUB", "50000000"))  # 50 ₽ по умолчанию на MVP
    deg = getattr(request.app.state, "degradation", None)
    breaker_open = bool(getattr(deg, "breaker_open", False)) if deg is not None else False

    cache_hit_est = round(
        max(0.0, 1.0 - (llm_traces / max(1, total_traces))) if total_traces > 0 and cache_entries > 0 else 0.0,
        4,
    )

    return {
        "index_version": get_index_version(),
        "config_version": cfg_ver,
        "degradation_level": chaos["degradation_level"],
        "chaos_flags": {
            "force_disable_llm": chaos["force_disable_llm"],
            "force_disable_dense": chaos["force_disable_dense"],
            "force_disable_overlay": chaos["force_disable_overlay"],
        },
        "breaker_open": breaker_open,
        "daily_llm_spend_micro_rub": daily_spend,
        "daily_llm_budget_micro_rub": daily_budget,
        "daily_llm_calls": daily_llm_calls,
        "latency_p50_ms": p50_ms,
        "latency_p95_ms": p95_ms,
        "n_traces": total_traces,
        "qu_cache_entries": cache_entries,
        "qu_cache_hit_rate_est": cache_hit_est,
        "data_tier": "derived",
    }


@router.post("/chaos")
def update_chaos_flags(body: ChaosPatchIn, request: Request) -> Dict[str, Any]:
    if body.degradation_level is not None and body.degradation_level.upper() not in ("L0", "L1", "L2", "L3"):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "INVALID_DEGRADATION_LEVEL",
                "message": "degradation_level должен быть L0, L1, L2 или L3",
                "trace_id": None,
                "details": None,
            },
        )
    updated = set_chaos_state(
        request=request,
        force_disable_llm=body.force_disable_llm,
        force_disable_dense=body.force_disable_dense,
        force_disable_overlay=body.force_disable_overlay,
        degradation_level=body.degradation_level,
    )
    return {"status": "ok", **updated}


@router.get("/cost")
def get_cost_summary() -> Dict[str, Any]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT purpose, provider, model, tokens_in, tokens_cached, tokens_out,
                   latency_ms, cost_micro_rub, status, ts
            FROM cost_ledger
            ORDER BY id DESC
            """
        ).fetchall()

        n_search_traces = int(conn.execute("SELECT COUNT(*) FROM search_traces").fetchone()[0])

    if not rows:
        return {
            "total_calls": 0,
            "total_cost_micro_rub": 0,
            "total_cost_rub": 0.0,
            "avg_cost_per_call_micro_rub": 0,
            "p50_cost_micro_rub": 0,
            "p95_cost_micro_rub": 0,
            "avg_cost_per_search_micro_rub": 0,
            "avg_cost_per_search_rub": 0.0,
            "n_search_traces": n_search_traces,
            "by_purpose": [],
            "csv_report_path": "eval/reports/cost_report.csv",
            "csv_exists": Path("eval/reports/cost_report.csv").exists(),
            "data_tier": "derived",
        }

    costs = [int(r["cost_micro_rub"]) for r in rows]
    total_cost = sum(costs)

    by_purpose_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        p = str(r["purpose"])
        bucket = by_purpose_map.setdefault(
            p,
            {
                "purpose": p,
                "calls": 0,
                "ok_calls": 0,
                "tokens_in": 0,
                "tokens_cached": 0,
                "tokens_out": 0,
                "cost_micro_rub": 0,
                "latencies": [],
            },
        )
        bucket["calls"] += 1
        if str(r["status"]) == "ok":
            bucket["ok_calls"] += 1
        bucket["tokens_in"] += int(r["tokens_in"])
        bucket["tokens_cached"] += int(r["tokens_cached"])
        bucket["tokens_out"] += int(r["tokens_out"])
        bucket["cost_micro_rub"] += int(r["cost_micro_rub"])
        bucket["latencies"].append(float(r["latency_ms"]))

    by_purpose: List[Dict[str, Any]] = []
    for p, b in sorted(by_purpose_map.items(), key=lambda kv: kv[1]["cost_micro_rub"], reverse=True):
        lats = b.pop("latencies")
        b["avg_latency_ms"] = round(sum(lats) / max(1, len(lats)), 2)
        b["cost_rub"] = round(b["cost_micro_rub"] / 1_000_000.0, 6)
        b["share_pct"] = round((b["cost_micro_rub"] * 100.0 / total_cost) if total_cost > 0 else 0.0, 2)
        by_purpose.append(b)

    avg_search_micro = int(round(total_cost / max(1, n_search_traces))) if n_search_traces > 0 else 0

    return {
        "total_calls": len(rows),
        "total_cost_micro_rub": total_cost,
        "total_cost_rub": round(total_cost / 1_000_000.0, 6),
        "avg_cost_per_call_micro_rub": int(round(total_cost / len(rows))),
        "p50_cost_micro_rub": int(round(float(np.percentile(costs, 50)))),
        "p95_cost_micro_rub": int(round(float(np.percentile(costs, 95)))),
        "avg_cost_per_search_micro_rub": avg_search_micro,
        "avg_cost_per_search_rub": round(avg_search_micro / 1_000_000.0, 6),
        "n_search_traces": n_search_traces,
        "by_purpose": by_purpose,
        "csv_report_path": "eval/reports/cost_report.csv",
        "csv_exists": Path("eval/reports/cost_report.csv").exists(),
        "data_tier": "derived",
    }


@router.get("/qc-report")
def get_qc_report() -> Dict[str, Any]:
    art_dir = get_artifacts_dir()
    candidates = []
    if art_dir is not None:
        candidates.append(art_dir / "qc_report.json")
    candidates.append(Path("data/interim/qc_report.json"))

    for p in candidates:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            data["data_tier"] = "real"
            return data

    raise HTTPException(
        status_code=404,
        detail={
            "error_code": "QC_REPORT_NOT_FOUND",
            "message": "qc_report.json не найден. Запустите пайплайн сборки индекса.",
            "trace_id": None,
            "details": None,
        },
    )