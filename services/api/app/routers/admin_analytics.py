"""
Роутер E-com & Руководитель (Экран 2):
- GET  /v1/admin/kpi?source=all|live|simulated
- POST /v1/admin/impact-model
- POST /v1/admin/ranking-weights
- GET  /v1/admin/eval-summary
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from services.api.app.routers.admin_deps import (
    get_artifacts_dir,
    get_current_config_version,
    get_db,
    load_default_ranking_weights,
    notify_overlay_reload,
    require_admin_token,
    utc_now_iso,
)

try:
    from services.api.app.schemas.admin import ImpactAssumptions, ImpactReport, ImpactScenarioOut
    from services.api.app.schemas.common import DataTier
except ImportError:
    from enum import Enum

    class DataTier(str, Enum):
        real = "real"
        derived = "derived"
        judged = "judged"
        simulated = "simulated"
        modeled = "modeled"

    class ImpactAssumptions(BaseModel):
        q_month: int = 15000
        f_b: float = 0.30
        f_n: Optional[float] = None
        cvr_s: float = 0.075
        kappa: float = 0.30
        aov_rub: Optional[float] = None
        margin: float = 0.20
        p_exit: float = 0.40

    class ImpactScenarioOut(BaseModel):
        scenario: Literal["pessimistic", "base", "optimistic"]
        recovered_sessions_month: float
        delta_orders_month: float
        delta_revenue_rub_month: float
        delta_profit_rub_month: float
        lost_orders_before_month: float
        lost_orders_after_month: float
        exits_before_month: float
        exits_after_month: float
        data_tier: DataTier = DataTier.modeled

    class ImpactReport(BaseModel):
        assumptions_used: ImpactAssumptions
        scenarios: List[ImpactScenarioOut]
        data_tier: DataTier = DataTier.modeled
        note: str = "Сценарный расчёт на допущениях, не измерение реального трафика"


router = APIRouter(prefix="/v1/admin", tags=["admin-analytics"], dependencies=[Depends(require_admin_token)])


class RankingWeightsIn(BaseModel):
    margin_weight: float = Field(ge=0.0, le=2.0)
    promo_weight: float = Field(ge=0.0, le=2.0)
    pop_weight: float = Field(ge=0.0, le=2.0)
    comment: Optional[str] = "Обновление весов из дашборда E-com"


def _load_latest_eval_report() -> Optional[Dict[str, Any]]:
    reports_dir = Path("eval/reports")
    if not reports_dir.exists():
        return None
    json_files = sorted(
        [p for p in reports_dir.glob("*.json") if not p.name.endswith(".tmp.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not json_files:
        return None
    try:
        return json.loads(json_files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _estimate_catalog_aov_rub() -> float:
    art_dir = get_artifacts_dir()
    fam_path = (art_dir / "families.parquet") if art_dir else Path("data/interim/families.parquet")
    if fam_path.exists():
        try:
            df = pd.read_parquet(fam_path, columns=["price_min_minor"])
            priced = df["price_min_minor"].dropna()
            priced = priced[priced > 0]
            if len(priced) > 0:
                return round(float(priced.median()) / 100.0, 2)
        except Exception:
            pass
    return 15000.0


def _extract_fn_from_eval(eval_data: Optional[Dict[str, Any]]) -> float:
    if not eval_data:
        return 0.08
    # Пытаемся достать 1 - Hit@4 для Full Smart (P3) из разных возможных структур отчёта
    checkpoints = eval_data.get("checkpoints") or eval_data.get("points") or {}
    if isinstance(checkpoints, dict):
        p3 = checkpoints.get("P3") or checkpoints.get("full_smart") or checkpoints.get("smart")
        if isinstance(p3, dict):
            hit4 = p3.get("hit_at_4") or p3.get("Hit@4")
            if hit4 is not None:
                return round(max(0.0, min(1.0, 1.0 - float(hit4))), 4)
    return 0.08


def _compute_kpi_for_source(conn, source_filter: Optional[str]) -> Dict[str, Any]:
    if source_filter:
        t_rows = conn.execute(
            "SELECT mode, hard_null, soft_null, latency_ms, source FROM search_traces WHERE source = ?",
            (source_filter,),
        ).fetchall()
        e_rows = conn.execute(
            "SELECT event_type, session_id, source FROM events WHERE source = ?",
            (source_filter,),
        ).fetchall()
    else:
        t_rows = conn.execute("SELECT mode, hard_null, soft_null, latency_ms, source FROM search_traces").fetchall()
        e_rows = conn.execute("SELECT event_type, session_id, source FROM events").fetchall()

    smart_traces = [r for r in t_rows if str(r["mode"]) == "smart"]
    legacy_traces = [r for r in t_rows if str(r["mode"]) == "legacy"]

    n_smart = len(smart_traces)
    n_legacy = len(legacy_traces)
    smart_hard_null = sum(int(r["hard_null"]) for r in smart_traces)
    smart_soft_null = sum(int(r["soft_null"]) for r in smart_traces)
    legacy_hard_null = sum(int(r["hard_null"]) for r in legacy_traces)

    counts: Dict[str, int] = {}
    for r in e_rows:
        et = str(r["event_type"])
        counts[et] = counts.get(et, 0) + 1

    searches = max(counts.get("search_issued", 0), len(t_rows))
    clicks = counts.get("result_click", 0) + counts.get("suggest_clicked", 0) + counts.get("cross_sell_click", 0)
    carts_or_quotes = counts.get("add_to_cart", 0) + counts.get("quote_request", 0)
    orders = counts.get("order", 0)
    abandons = counts.get("abandon", 0)
    gap_leads = counts.get("catalog_gap_lead", 0)

    tier = "real" if source_filter == "live" else "simulated"

    return {
        "n_searches_smart": n_smart,
        "n_searches_legacy": n_legacy,
        "hard_null_rate_smart": round(smart_hard_null / n_smart, 4) if n_smart else 0.0,
        "soft_null_rate_smart": round(smart_soft_null / n_smart, 4) if n_smart else 0.0,
        "hard_null_rate_legacy": round(legacy_hard_null / n_legacy, 4) if n_legacy else None,
        "abandon_rate": round(abandons / searches, 4) if searches else 0.0,
        "ctr": round(clicks / searches, 4) if searches else 0.0,
        "search_to_order_cvr": round(orders / searches, 4) if searches else 0.0,
        "funnel": {
            "search_issued": searches,
            "clicks": clicks,
            "add_to_cart_or_quote": carts_or_quotes,
            "orders": orders,
            "abandons": abandons,
            "catalog_gap_leads": gap_leads,
        },
        "data_tier": tier,
    }


@router.get("/kpi")
def get_admin_kpi(source: Literal["all", "live", "simulated"] = Query(default="all")) -> Dict[str, Any]:
    with get_db() as conn:
        live_stats = _compute_kpi_for_source(conn, "live")
        sim_stats = _compute_kpi_for_source(conn, "simulated")
        summary_stats = (
            live_stats if source == "live" else (sim_stats if source == "simulated" else _compute_kpi_for_source(conn, None))
        )

    eval_data = _load_latest_eval_report()
    f_n = _extract_fn_from_eval(eval_data)
    # Быстрая базовая оценка потерянных заказов по базовому сценарию A.10
    aov = _estimate_catalog_aov_rub()
    q_m = 15000
    f_b = 0.30
    cvr_s = 0.075
    kappa = 0.30
    lost_before = round(q_m * f_b * cvr_s * (1.0 - kappa), 1)
    lost_after = round(q_m * f_n * cvr_s * (1.0 - kappa), 1)

    return {
        "source_filter": source,
        "summary": summary_stats,
        "by_source": {
            "live": live_stats,
            "simulated": sim_stats,
        },
        "organizer_kpis": [
            {
                "id": "zero_result_rate",
                "title": "Доля запросов без результата (Smart hard/soft null)",
                "value": summary_stats["soft_null_rate_smart"],
                "hard_null": summary_stats["hard_null_rate_smart"],
                "legacy_value": summary_stats["hard_null_rate_legacy"],
                "data_tier": summary_stats["data_tier"],
                "caption": "На живых и симулированных трассах; эталон на SYN — в eval-summary",
            },
            {
                "id": "exit_rate_after_fail",
                "title": "Уходы после неудачного поиска (abandon)",
                "value": summary_stats["abandon_rate"],
                "data_tier": summary_stats["data_tier"],
                "caption": "Демо-трафик / модель при p_exit 30–50%",
            },
            {
                "id": "search_to_order_cvr",
                "title": "Конверсия поиска в заказ",
                "value": summary_stats["search_to_order_cvr"],
                "data_tier": summary_stats["data_tier"],
                "caption": "Демо-воронка (симулятор / живые клики)",
            },
            {
                "id": "lost_orders_avoided",
                "title": "Сокращение потерянных заказов / мес (базовый сценарий)",
                "value": round(max(0.0, lost_before - lost_after), 1),
                "before": lost_before,
                "after": lost_after,
                "aov_proxy_rub": aov,
                "data_tier": "modeled",
                "caption": "Сценарный расчёт A.10 (Q=15k, f_b=0.30, CVR_s=7.5%)",
            },
        ],
    }


@router.post("/impact-model", response_model=ImpactReport)
def calculate_impact_model(assumptions: ImpactAssumptions) -> ImpactReport:
    eval_data = _load_latest_eval_report()
    f_n = assumptions.f_n if assumptions.f_n is not None else _extract_fn_from_eval(eval_data)
    aov = assumptions.aov_rub if assumptions.aov_rub is not None else _estimate_catalog_aov_rub()

    used = ImpactAssumptions(
        q_month=assumptions.q_month,
        f_b=assumptions.f_b,
        f_n=f_n,
        cvr_s=assumptions.cvr_s,
        kappa=assumptions.kappa,
        aov_rub=aov,
        margin=assumptions.margin,
        p_exit=assumptions.p_exit,
    )

    # 3 детерминированных сценария строго по формулам CONTRACTS.md A.10
    params_by_scenario = [
        (
            "pessimistic",
            round(used.q_month * (10000.0 / 15000.0)),
            max(0.05, used.f_b - 0.10),
            max(0.01, used.cvr_s * (0.05 / 0.075)),
            min(0.85, used.kappa + 0.20),
            aov * 0.7,
            max(0.05, used.margin - 0.05),
            max(0.10, used.p_exit - 0.10),
        ),
        (
            "base",
            used.q_month,
            used.f_b,
            used.cvr_s,
            used.kappa,
            aov,
            used.margin,
            used.p_exit,
        ),
        (
            "optimistic",
            round(used.q_month * (20000.0 / 15000.0)),
            min(0.80, used.f_b + 0.10),
            min(0.50, used.cvr_s * (0.10 / 0.075)),
            max(0.05, used.kappa - 0.15),
            aov * 1.3,
            min(0.60, used.margin + 0.05),
            min(0.90, used.p_exit + 0.10),
        ),
    ]

    scenarios: List[ImpactScenarioOut] = []
    for name, q_m, fb, cvr, kap, aov_s, m_s, p_ex in params_by_scenario:
        recovered = q_m * max(0.0, fb - f_n)
        d_orders = recovered * cvr * (1.0 - kap)
        d_rev = d_orders * aov_s
        d_prof = d_rev * m_s
        lost_b = q_m * fb * cvr * (1.0 - kap)
        lost_a = q_m * f_n * cvr * (1.0 - kap)
        exits_b = q_m * fb * p_ex
        exits_a = q_m * f_n * p_ex

        scenarios.append(
            ImpactScenarioOut(
                scenario=name,  # type: ignore[arg-type]
                recovered_sessions_month=round(recovered, 1),
                delta_orders_month=round(d_orders, 1),
                delta_revenue_rub_month=round(d_rev, 0),
                delta_profit_rub_month=round(d_prof, 0),
                lost_orders_before_month=round(lost_b, 1),
                lost_orders_after_month=round(lost_a, 1),
                exits_before_month=round(exits_b, 1),
                exits_after_month=round(exits_a, 1),
                data_tier=DataTier.modeled,
            )
        )

    return ImpactReport(
        assumptions_used=used,
        scenarios=scenarios,
        data_tier=DataTier.modeled,
        note="Сценарный расчёт на допущениях, не измерение реального трафика",
    )


@router.post("/ranking-weights")
def update_ranking_weights(body: RankingWeightsIn, request: Request) -> Dict[str, Any]:
    with get_db() as conn:
        cur_ver = get_current_config_version(conn)
        prev = conn.execute(
            "SELECT overlay_snapshot_json, ranking_weights_json FROM config_versions WHERE config_version = ?",
            (cur_ver,),
        ).fetchone()

        overlay_snap = prev["overlay_snapshot_json"] if prev else "[]"
        weights = json.loads(prev["ranking_weights_json"]) if prev else load_default_ranking_weights()

        weights["margin_proxy"] = round(float(body.margin_weight), 4)
        weights["promo"] = round(float(body.promo_weight), 4)
        weights["popularity"] = round(float(body.pop_weight), 4)

        new_ver = cur_ver + 1
        conn.execute(
            """
            INSERT INTO config_versions (
                config_version, created_at, author, ticket_id,
                action, comment, overlay_snapshot_json, ranking_weights_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ver,
                utc_now_iso(),
                "ecom_manager",
                None,
                "manual_edit",
                body.comment or "Обновление весов маржи/акций/популярности",
                overlay_snap,
                json.dumps(weights, ensure_ascii=False),
            ),
        )
        conn.commit()
        notify_overlay_reload(request, conn)

    return {
        "config_version": new_ver,
        "ranking_weights": weights,
        "status": "applied",
    }


@router.get("/eval-summary")
def get_eval_summary() -> Dict[str, Any]:
    data = _load_latest_eval_report()
    if data is not None:
        return data
    return {
        "status": "not_run_yet",
        "message": "Отчёт eval пока не сформирован (ожидается в eval/reports/*.json)",
        "checkpoints": {},
        "gold10": None,
        "data_tier": "derived",
    }