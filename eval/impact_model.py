"""Детерминированный расчёт бизнес-эффекта по 3 сценариям (CONTRACTS.md A.10, tier=modeled)."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

import pandas as pd

from services.api.app.schemas import DataTier, ImpactAssumptions, ImpactReport, ImpactScenarioOut
from services.api.core.index_state import IndexState


def estimate_catalog_aov_rub(index_state: IndexState, fallback_rub: float = 25_000.0) -> float:
    if not index_state.is_loaded or index_state.offers is None or "price_minor" not in index_state.offers.columns:
        return fallback_rub
    prices = index_state.offers["price_minor"].dropna()
    valid = [float(p) / 100.0 for p in prices.tolist() if float(p) > 0]
    if not valid:
        return fallback_rub
    return round(float(statistics.median(valid)), 2)


def compute_impact_report(
    *,
    f_n: float,
    aov_rub: Optional[float] = None,
    index_state: Optional[IndexState] = None,
    base_assumptions: Optional[ImpactAssumptions] = None,
) -> ImpactReport:
    aov = float(aov_rub if aov_rub is not None else estimate_catalog_aov_rub(index_state)) if (aov_rub or index_state) else 25_000.0
    assump = base_assumptions or ImpactAssumptions(f_n=round(f_n, 4), aov_rub=aov)
    f_n_val = float(assump.f_n if assump.f_n is not None else f_n)

    presets = [
        ("pessimistic", 10_000, 0.20, 0.050, 0.50, 0.7 * aov, 0.15, 0.30),
        ("base", assump.q_month, assump.f_b, assump.cvr_s, assump.kappa, aov, assump.margin, assump.p_exit),
        ("optimistic", 20_000, 0.40, 0.100, 0.15, 1.3 * aov, 0.25, 0.50),
    ]

    scenarios: List[ImpactScenarioOut] = []
    for name, q_m, f_b, cvr_s, kappa, scen_aov, margin, p_exit in presets:
        recovered = q_m * max(0.0, f_b - f_n_val)
        delta_orders = recovered * cvr_s * (1.0 - kappa)
        delta_rev = delta_orders * scen_aov
        delta_profit = delta_rev * margin
        lost_before = q_m * f_b * cvr_s * (1.0 - kappa)
        lost_after = q_m * f_n_val * cvr_s * (1.0 - kappa)
        exits_before = q_m * f_b * p_exit
        exits_after = q_m * f_n_val * p_exit
        scenarios.append(
            ImpactScenarioOut(
                scenario=name,  # type: ignore[arg-type]
                recovered_sessions_month=round(recovered, 1),
                delta_orders_month=round(delta_orders, 1),
                delta_revenue_rub_month=round(delta_rev, 0),
                delta_profit_rub_month=round(delta_profit, 0),
                lost_orders_before_month=round(lost_before, 1),
                lost_orders_after_month=round(lost_after, 1),
                exits_before_month=round(exits_before, 1),
                exits_after_month=round(exits_after, 1),
                data_tier=DataTier.modeled,
            )
        )

    return ImpactReport(
        assumptions_used=ImpactAssumptions(
            q_month=assump.q_month,
            f_b=assump.f_b,
            f_n=round(f_n_val, 4),
            cvr_s=assump.cvr_s,
            kappa=assump.kappa,
            aov_rub=round(aov, 2),
            margin=assump.margin,
            p_exit=assump.p_exit,
        ),
        scenarios=scenarios,
        data_tier=DataTier.modeled,
    )