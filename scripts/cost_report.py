"""Выгрузка unit-экономики одного запроса в eval/reports/cost_report.csv (CONTRACTS.md A.12)."""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from services.api.core.cost_meter import SECONDS_PER_MONTH, connect_db, default_db_path
from services.api.app.main import _load_dotenv_defaults
_load_dotenv_defaults()

def generate_cost_report(
    out_csv: Path = Path("eval/reports/cost_report.csv"),
    *,
    q_month: int = 15_000,
    vps_price_rub_month: float = 2_500.0,
    vps_vcpu: int = 4,
    amort_months: int = 3,
    cpu_ms_per_query: float = 120.0,
) -> Path:
    vps_price = float(os.getenv("VPS_PRICE_RUB_MONTH", str(vps_price_rub_month)))
    vcpu = int(os.getenv("VPS_VCPU", str(vps_vcpu)))

    conn = connect_db(default_db_path())
    try:
        rows = conn.execute(
            """
            SELECT c.purpose, c.model, c.tokens_in, c.tokens_cached, c.tokens_out,
                   c.latency_ms, c.cost_micro_rub, c.status,
                   p.price_source_url, p.created_at
            FROM cost_ledger c
            LEFT JOIN price_snapshots p ON p.snapshot_id = c.price_snapshot_id
            """
        ).fetchall()
        n_traces_row = conn.execute("SELECT COUNT(*) FROM search_traces WHERE mode = 'smart'").fetchone()
        n_search_traces = max(1, int(n_traces_row[0] or 0))
    finally:
        conn.close()

    by_purpose: Dict[str, List[tuple]] = {}
    for r in rows:
        by_purpose.setdefault(str(r[0]), []).append(tuple(r))

    # Расчёт по формулам A.12
    qu_rows = by_purpose.get("qu", [])
    qu_costs_rub = [r[6] / 1_000_000.0 for r in qu_rows]
    # Доля запросов, эскалированных в LLM (если трасс мало, берём ориентир 0.15)
    p_esc_eff = min(1.0, len(qu_rows) / max(n_search_traces, 500)) if qu_rows else 0.15
    avg_qu_call_rub = float(np.mean(qu_costs_rub)) if qu_costs_rub else 0.12
    c_llm_per_query = round(p_esc_eff * avg_qu_call_rub, 6)

    c_cpu_sec = vps_price / (max(1, vcpu) * SECONDS_PER_MONTH)
    c_cpu_per_query = round((cpu_ms_per_query / 1000.0) * c_cpu_sec, 6)
    c_fixed_per_query = round(vps_price / max(1, q_month), 6)

    offline_purposes = ("classify_category", "enrich_family", "vendor_extract", "judge", "synth_queries")
    offline_total_rub = sum(r[6] / 1_000_000.0 for p in offline_purposes for r in by_purpose.get(p, []))
    c_off_per_query = round(offline_total_rub / (max(1, amort_months) * max(1, q_month)), 6)

    agent_rows = by_purpose.get("agent_diagnose", [])
    agent_total_rub = sum(r[6] / 1_000_000.0 for r in agent_rows)
    c_agent_per_query = round(agent_total_rub / max(1, q_month), 6)

    total_c_q = max(1e-9, c_llm_per_query + c_cpu_per_query + c_fixed_per_query + c_off_per_query + c_agent_per_query)

    price_src = qu_rows[-1][8] if qu_rows and qu_rows[-1][8] else "https://yandex.cloud/ru/docs/ai-studio/pricing"
    price_date = (qu_rows[-1][9] or "2026-11-26")[:10] if qu_rows else "2026-11-26"

    items = [
        ("C_llm (Online QU)", "LLMGateway role=FAST (qwen3.6-35b-a3b)", "call/query", round(p_esc_eff, 4), round(avg_qu_call_rub, 6), price_src, price_date, c_llm_per_query, "measured" if qu_rows else "assumed"),
        ("C_cpu (Retrieval + CPU encoder)", "vCPU seconds (BM25F + BGE-M3 query)", "cpu_sec/query", round(cpu_ms_per_query / 1000.0, 4), round(c_cpu_sec, 6), "VPS tariff", price_date, c_cpu_per_query, "assumed"),
        ("C_fixed_loaded (VPS hosting)", f"Q_month={q_month} req/mo", "1/Q_month", round(1.0 / q_month, 8), round(vps_price, 2), "VPS tariff", price_date, c_fixed_per_query, "assumed"),
        ("C_off (Offline enrich/eval amortized)", f"H={amort_months} mo amortization", "1/(H*Q_month)", round(1.0 / (amort_months * q_month), 8), round(offline_total_rub, 4), price_src, price_date, c_off_per_query, "measured" if offline_total_rub > 0 else "assumed"),
        ("C_agent (Diagnostic worker)", "cost_ledger purpose=agent_diagnose", "1/Q_month", round(1.0 / q_month, 8), round(agent_total_rub, 4), price_src, price_date, c_agent_per_query, "measured" if agent_rows else "assumed"),
    ]

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["component", "driver", "unit", "quantity_per_query", "unit_price_rub", "price_source", "price_date", "cost_per_query_rub", "share_pct", "basis"])
        for comp, drv, unit, qty, up, src, dt, cpq, basis in items:
            share = round((cpq / total_c_q) * 100.0, 2)
            writer.writerow([comp, drv, unit, qty, up, src, dt, cpq, share, basis])

    print(f"cost_report.csv saved -> {out_csv}")
    print(f"Total C_q = {total_c_q:.4f} RUB/query (C_llm={c_llm_per_query:.4f}, C_fixed={c_fixed_per_query:.4f}, C_cpu={c_cpu_per_query:.4f}, C_off={c_off_per_query:.4f}, C_agent={c_agent_per_query:.4f})")
    if qu_costs_rub:
        print(f"QU call stats (n={len(qu_costs_rub)}): mean={np.mean(qu_costs_rub):.4f} RUB, p50={np.percentile(qu_costs_rub, 50):.4f}, p95={np.percentile(qu_costs_rub, 95):.4f}")
    return out_csv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--q-month", type=int, default=15000)
    args = parser.parse_args()
    generate_cost_report(q_month=args.q_month)


if __name__ == "__main__":
    main()