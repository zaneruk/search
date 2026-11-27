# LLM-judge по top-5 на SYN-B + таблица для ручного аудита
"""LLM-judge для SYN-B top-5 (CONTRACTS.md A.2, A.12) + выгрузка выборки 30–40 строк для ручного аудита."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from services.api.app.schemas import JudgeOutput, SearchRequest
from services.api.core.index_state import get_index_state
from services.api.core.overlay import get_overlay_store
from services.api.llm.gateway import LLMGateway, LLMRole, wrap_user_input
from services.api.pipeline import execute_search


async def run_judge_async(
    syn_b_path: Path = Path("eval/datasets/syn_b/queries.jsonl"),
    out_json: Path = Path("eval/reports/syn_b_judge.json"),
    audit_csv: Path = Path("eval/reports/syn_b_manual_audit_40.csv"),
    *,
    max_queries: int = 80,
    top_k: int = 5,
) -> Dict[str, Any]:
    if not syn_b_path.is_file():
        raise FileNotFoundError(f"SYN-B file not found: {syn_b_path}")

    queries = [json.loads(line) for line in syn_b_path.read_text(encoding="utf-8").splitlines() if line.strip()][:max_queries]
    prompt = Path("prompts/judge/v1.md").read_text(encoding="utf-8").strip()
    idx = get_index_state()
    ov = get_overlay_store()
    ov.load()
    gw = LLMGateway()
    await gw.startup()

    judged_rows: List[Dict[str, Any]] = []
    audit_rows: List[Dict[str, Any]] = []

    try:
        for q_rec in queries:
            q = q_rec["query"]
            resp = await execute_search(
                SearchRequest(query=q, session_id="ss_judge", options={"llm_enabled": True, "include_trace": False}),
                persist_events=False,
                index_state=idx,
                overlay_store=ov,
                gateway=gw,
            )
            items = [it for sec in resp.sections for it in sec.items][:top_k]
            grades: List[int] = []

            for rank, item in enumerate(items, start=1):
                user_msg = (
                    f"Запрос: {wrap_user_input(q)} (класс: {q_rec.get('query_class')})\n"
                    f"Товар #{rank}: {item.canonical_name} | Вендор: {item.vendor} | "
                    f"Класс: {item.product_class.value} | Тип: {item.product_type} | Метка: {item.relevance_label.value}"
                )
                res = await gw.complete_json(
                    purpose="judge",
                    role=LLMRole.SYNTH,
                    system=prompt,
                    user=user_msg,
                    schema=JudgeOutput,
                    offline=True,
                    timeout_ms=15000,
                    max_tokens=120,
                    use_cache=False,
                )
                grade = res.parsed.grade if (res.ok and res.parsed) else 0
                reason = res.parsed.reason if (res.ok and res.parsed) else (res.error or "judge_error")
                grades.append(grade)
                if rank == 1 and len(audit_rows) < 40:
                    audit_rows.append(
                        {
                            "id": q_rec.get("id"),
                            "query": q,
                            "query_class": q_rec.get("query_class"),
                            "top1_name": item.canonical_name,
                            "top1_class": item.product_class.value,
                            "llm_grade": grade,
                            "llm_reason": reason,
                            "human_grade": "",
                        }
                    )

            judged_rows.append(
                {
                    "id": q_rec.get("id"),
                    "query": q,
                    "query_class": q_rec.get("query_class"),
                    "grades_top5": grades,
                    "hit4_judged": any(g >= 2 for g in grades[:4]),
                    "top1_grade": grades[0] if grades else 0,
                }
            )
    finally:
        await gw.aclose()

    hit4 = sum(1 for r in judged_rows if r["hit4_judged"])
    summary = {
        "n_queries": len(judged_rows),
        "hit4_judged": round(hit4 / len(judged_rows), 4) if judged_rows else 0.0,
        "mean_top1_grade": round(sum(r["top1_grade"] for r in judged_rows) / len(judged_rows), 4) if judged_rows else 0.0,
        "data_tier": "judged",
        "rows": judged_rows,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with audit_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["id", "query", "query_class", "top1_name", "top1_class", "llm_grade", "llm_reason", "human_grade"])
        writer.writeheader()
        writer.writerows(audit_rows)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-queries", type=int, default=80)
    args = parser.parse_args()
    res = asyncio.run(run_judge_async(max_queries=args.max_queries))
    print(f"SYN-B LLM-judge done: n={res['n_queries']} | Hit@4 (judged)={res['hit4_judged']} | mean_top1={res['mean_top1_grade']}")


if __name__ == "__main__":
    main()