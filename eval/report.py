"""Генератор eval/reports/REPORT.md по протоколу честности CONTRACTS.md (A.11, A.13)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def generate_markdown_report(
    eval_json_path: Path = Path("eval/reports/eval_report.json"),
    judge_json_path: Path = Path("eval/reports/syn_b_judge.json"),
    out_md_path: Path = Path("eval/reports/REPORT.md"),
) -> Path:
    rep: Dict[str, Any] = json.loads(eval_json_path.read_text(encoding="utf-8"))
    judge: Dict[str, Any] = json.loads(judge_json_path.read_text(encoding="utf-8")) if judge_json_path.is_file() else {}

    g10 = rep["gold10"]
    cps = rep["checkpoints"]
    sug = rep["suggest"]
    margin = rep["margin_sensitivity"]
    imp = rep["impact_model"]

    lines = [
        f"# Отчёт оценки качества «Умного поиска» ({rep['run_id']})",
        "",
        f"- **git_sha:** `{rep['git_sha']}` | **index_version:** `{rep['index_version']}` | **config_version:** `{rep['config_version']}` | **embedding_model:** `{rep['embedding_model']}` | **seed:** `{rep['seed']}`",
        "",
        "## 1. GOLD10 Smoke-тест (`data_tier = real`)",
        f"- **Результат:** {g10['hit4_count']} из {g10['n']} (Hit@4 = {g10['hit4']}, Hit@1 = {g10['hit1']}; n < 30 — описательно, без ДИ; нейтральный контекст, без gold-хаков по R6).",
        "",
        "## 2. Сравнение 4 конфигураций на holdout SYN-A (`data_tier = derived`, n = " + str(rep["holdout_size"]) + ")",
        "",
        "| Точка | Hit@1 | Hit@4 (95% Уилсон) | MRR@10 | nDCG@10 | Hard-null | Soft-null | Typo-1 Hit@4 | FCR | p95, мс | Инварианты |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cp_id, cp in cps.items():
        ci = cp["hit4"]["ci_95"]
        ci_str = f"[{ci[0]}; {ci[1]}]" if ci else "n<30"
        lines.append(
            f"| **{cp_id}** ({cp['label']}) | {cp['hit1']['value']} | {cp['hit4']['value']} {ci_str} | {cp['mrr10']} | {cp['ndcg10']} | "
            f"{cp['hard_null_rate']['value']} | {cp['soft_null_rate']['value']} | {cp['typo1_recovery4']['value']} | "
            f"{cp['fcr']['value']} | {cp['latency_p95_ms']} | {cp['invariants_pass_rate'] * 100:.1f}% |"
        )

    lines.extend(["", "## 3. Статистическая значимость (парный бутстрэп 10 000 + Макнемар, `data_tier = derived`)"])
    for cname, comp in rep.get("comparisons", {}).items():
        hb = comp["hit4_bootstrap"]
        mc = comp["hit4_mcnemar"]
        lines.append(
            f"- **{cname}:** ΔHit@4 = `{hb['delta']:+}` (95% ДИ `[{hb['ci_low']}; {hb['ci_high']}]`, McNemar p=`{mc['p_value']}`, n01={mc['n01']}, n10={mc['n10']})"
        )

    if judge:
        lines.extend([
            "",
            "## 4. Сложные запросы SYN-B (`data_tier = judged`)",
            f"- **LLM-judge (n={judge.get('n_queries', 0)}):** Hit@4 (grade ≥ 2) = `{judge.get('hit4_judged')}`, средний grade top-1 = `{judge.get('mean_top1_grade')}`.",
        ])

    lines.extend([
        "",
        "## 5. Typeahead (`/v1/suggest`, `data_tier = derived`) и маржинальность (A.9)",
        f"- **Suggest latency:** p50 = `{sug['latency_p50_ms']} мс`, p95 = `{sug['latency_p95_ms']} мс` (гейт ≤ 60 мс).",
        f"- **Чувствительность к марже:** nDCG@10 (margin=0: `{margin['ndcg10']['margin_0']}`, default: `{margin['ndcg10']['margin_default']}`, 3x: `{margin['ndcg10']['margin_3x']}`), Δ=`{margin['delta_default_vs_0']}` (гейт ≥ −0.01: `{margin['passed_a9_gate']}`).",
        "",
        "## 6. Сценарный бизнес-эффект (`data_tier = modeled`)",
        f"> Примечание: {imp['note']} (f_n = {imp['assumptions_used']['f_n']}, AOV = {imp['assumptions_used']['aov_rub']} ₽).",
        "",
        "| Сценарий | Восст. сессий/мес | Доп. заказы/мес | Доп. выручка, ₽/мес | Доп. прибыль, ₽/мес |",
        "|---|---:|---:|---:|---:|",
    ])
    for sc in imp["scenarios"]:
        lines.append(
            f"| {sc['scenario']} | {sc['recovered_sessions_month']} | {sc['delta_orders_month']} | "
            f"{sc['delta_revenue_rub_month']:,.0f} | {sc['delta_profit_rub_month']:,.0f} |"
        )

    out_md_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_md_path


def main() -> None:
    p = generate_markdown_report()
    print(f"REPORT.md generated -> {p}")


if __name__ == "__main__":
    main()