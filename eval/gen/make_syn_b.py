 # ~80 сложных NL/сленг-запросов через LLM (role=SYNTH)
"""Генератор SYN-B (CONTRACTS.md A.2, A.3): ~80 сложных запросов (slang, abbreviation, nl_task,
head_noun_hardware, ambiguous, analog) через LLMRole.SYNTH (отдельным промптом) с дедупликацией
относительно алиасов индекса (`dedupe_vs_index.py`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from eval.gen.dedupe_vs_index import dedupe_queries_vs_index
from services.api.core.index_state import get_index_state
from services.api.llm.gateway import LLMGateway, LLMRole


class _SynthItem(BaseModel):
    query: str
    query_class: str
    hint_product_type: str = ""


class _SynthBatch(BaseModel):
    items: List[_SynthItem] = Field(default_factory=list)


_QUOTAS = {
    "slang": 15,
    "abbreviation": 15,
    "nl_task": 25,
    "head_noun_hardware": 10,
    "ambiguous": 10,
    "analog": 5,
}

# Резервный пул, если запуск идёт полностью без LLM (--no-llm)
_FALLBACK_POOL: List[Dict[str, str]] = [
    {"query": "антивирь для сетки офиса", "query_class": "slang", "hint_product_type": "Защита конечных точек (EDR/AV)"},
    {"query": "прога для удаленки с сотрудниками", "query_class": "slang", "hint_product_type": "Удаленный доступ"},
    {"query": "резервка на виртуальные машины", "query_class": "slang", "hint_product_type": "Резервное копирование"},
    {"query": "защита от шифровальщиков для файлового сервера", "query_class": "nl_task", "hint_product_type": "Защита конечных точек (EDR/AV)"},
    {"query": "чем закрыть требования 152 фз по защите персональных данных", "query_class": "nl_task", "hint_product_type": "Средства криптографической защиты (СКЗИ)"},
    {"query": "система мониторинга событий безопасности в корпоративной сети", "query_class": "nl_task", "hint_product_type": "SIEM и управление инцидентами"},
    {"query": "сервер", "query_class": "head_noun_hardware", "hint_product_type": "Серверное оборудование"},
    {"query": "коммутатор", "query_class": "head_noun_hardware", "hint_product_type": "Сетевое оборудование"},
    {"query": "отечественный аналог microsoft office для госкомпании", "query_class": "analog", "hint_product_type": "Офисные пакеты и редакторы"},
    {"query": "российская замена зарубежному почтовому серверу", "query_class": "analog", "hint_product_type": "ВКС, корпоративная почта и коммуникации"},
]


async def generate_syn_b_async(
    out_path: Path = Path("eval/datasets/syn_b/queries.jsonl"),
    *,
    use_llm: bool = True,
) -> List[Dict[str, Any]]:
    idx = get_index_state()
    ptypes: List[str] = []
    if idx.is_loaded and idx.families is not None and "product_type" in idx.families.columns:
        ptypes = [str(x) for x in idx.families["product_type"].dropna().value_counts().head(25).index.tolist()]

    raw_items: List[Dict[str, Any]] = []
    if use_llm:
        prompt_path = Path("prompts/synth_queries/v1.md")
        system = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.is_file() else "Сгенерируй B2B запросы JSON {items}."
        gw = LLMGateway()
        await gw.startup()
        try:
         for cls_name, cnt in _QUOTAS.items():
             remaining = cnt + 4
             while remaining > 0:
                 batch_n = min(15, remaining)
                 remaining -= batch_n
                 user = (
                     f"Сгенерируй ровно {batch_n} разных запросов класса '{cls_name}'.\n"
                     f"Примеры product_type каталога: {', '.join(ptypes[:15])}.\n"
                     f"Формат: {{\"items\":[{{\"query\":\"...\",\"query_class\":\"{cls_name}\",\"hint_product_type\":\"...\"}}]}}"
                 )
                 res = await gw.complete_json(
                     purpose="synth_queries",
                     role=LLMRole.SYNTH,
                     system=system,
                     user=user,
                     schema=_SynthBatch,
                     offline=True,
                     timeout_ms=30000,
                     max_tokens=2200,
                     temperature=0.5,
                     use_cache=False,
                 )
                 if res.ok and res.parsed:
                     for it in res.parsed.items:
                         raw_items.append(it.model_dump())
                 else:
                     print(f"[SYN-B] batch warning for {cls_name}: {res.error}")
        finally:
            await gw.aclose()

    if not raw_items:
        raw_items = list(_FALLBACK_POOL)

    deduped = dedupe_queries_vs_index(raw_items, idx)

    # Приводим к квотам
    final: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {k: 0 for k in _QUOTAS}
    for it in deduped:
        qcls = it.get("query_class", "nl_task")
        if qcls not in _QUOTAS:
            qcls = "nl_task"
        if counts[qcls] >= _QUOTAS[qcls]:
            continue
        counts[qcls] += 1
        final.append(
            {
                "id": f"synb_{len(final) + 1:04d}",
                "query": str(it["query"]).strip(),
                "query_class": qcls,
                "hint_product_type": str(it.get("hint_product_type") or ""),
                "split": "holdout" if (len(final) % 10) >= 5 else "dev",
                "data_tier": "derived",
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in final:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Генератор набора SYN-B (LLM SYNTH + дедупликация)")
    parser.add_argument("--out", default="eval/datasets/syn_b/queries.jsonl")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    recs = asyncio.run(generate_syn_b_async(Path(args.out), use_llm=not args.no_llm))
    by_cls: Dict[str, int] = {}
    for r in recs:
        by_cls[r["query_class"]] = by_cls.get(r["query_class"], 0) + 1
    print(f"SYN-B generated: {len(recs)} queries -> {args.out}")
    print("Classes:", by_cls)


if __name__ == "__main__":
    main()