#!/usr/bin/env python3
"""
Точечная LLM-классификация ТОЛЬКО неразмеченных категорий (где classify_confident == False).
Использует единый шлюз Yandex AI Studio (роль strong = aliceai-llm) и пишет расходы в cost_ledger.
"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from pydantic import BaseModel

from pipelines.enrich.classify_rules import load_taxonomy_allowed
from services.api.llm.gateway import call_llm_structured, get_llm_credentials

VALID_CLASSES = {"software", "hardware", "cloud_service", "subscription", "certificate", "training/webinar", "other"}


class ClassifyCategoryOut(BaseModel):
    product_class: str
    product_type: str


def append_cost_log(
    log_path: Path,
    purpose: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    status: str,
) -> None:
    """Оставлено для обратной совместимости импортов; основной учёт идёт в SQLite cost_ledger через gateway.py."""
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-классификация спорных категорий (Yandex AI Studio)")
    parser.add_argument("--families", default="data/interim/families.parquet")
    parser.add_argument("--taxonomy", default="config/taxonomy.yaml")
    parser.add_argument("--cache", default="data/interim/cache_classify_cat.json")
    parser.add_argument("--max-categories", type=int, default=300)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    df = pd.read_parquet(args.families)
    if "classify_confident" not in df.columns:
        print("Колонка classify_confident не найдена. Сначала запустите classify_rules.py.")
        return

    ambig = df[~df["classify_confident"]]
    if len(ambig) == 0:
        print("Все категории уже размечены правилами! Вызов LLM не требуется.")
        return

    cat_groups = (
        ambig.groupby("category_id")
        .agg(
            n_fam=("family_id", "count"),
            category_path=("category_path", "first"),
            sample_names=("canonical_name", lambda s: list(s.head(4))),
        )
        .sort_values("n_fam", ascending=False)
        .head(args.max_categories)
    )
    print(f"Неразмеченных семейств: {len(ambig)}, уникальных категорий к разметке: {len(cat_groups)}")

    cache_path = Path(args.cache)
    cache: Dict[str, Dict[str, str]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )

    allowed_types = load_taxonomy_allowed(Path(args.taxonomy))
    allowed_set = set(allowed_types)
    prompt_file = Path("prompts/classify_category/v1.md")
    system_prompt = (
        prompt_file.read_text(encoding="utf-8")
        if prompt_file.exists()
        else "Classify category into product_class and product_type from allowed_product_types. Output JSON only."
    )
    system_prompt += f"\n\nallowed_product_types: {json.dumps(allowed_types, ensure_ascii=False)}"

    _, _, api_key, _ = get_llm_credentials()
    if not api_key:
        print("API-ключ LLM не найден в .env — пропускаю вызовы LLM.")
        return

    todo: List[Tuple[str, Any]] = [(str(cid), crow) for cid, crow in cat_groups.iterrows() if str(cid) not in cache]
    print(f"В кэше: {len(cat_groups) - len(todo)}, новых вызовов LLM: {len(todo)}")

    cache_lock = threading.Lock()
    done_cnt = 0

    def process_one(item: Tuple[str, Any]) -> Tuple[str, Optional[Dict[str, str]]]:
        cid, crow = item
        payload = (
            f"<user_input>\n"
            f"category_path: {crow['category_path']}\n"
            f"examples: {json.dumps(crow['sample_names'], ensure_ascii=False)}\n"
            f"</user_input>"
        )
        parsed, meta = call_llm_structured(
            role="strong",
            purpose="classify_category",
            system_prompt=system_prompt,
            user_payload=payload,
            schema_cls=ClassifyCategoryOut,
            max_tokens_override=120,
        )
        if not parsed:
            return cid, None
        pc = str(parsed.get("product_class", "software")).strip()
        pt = str(parsed.get("product_type", "Прочее ПО")).strip()
        if pc not in VALID_CLASSES:
            pc = "software"
        if pt not in allowed_set:
            pt = next((a for a in allowed_types if pt.lower() in a.lower()), allowed_types[0])
        return cid, {"product_class": pc, "product_type": pt}

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(process_one, it) for it in todo]
            for fut in as_completed(futures):
                cid, res = fut.result()
                if res is not None:
                    with cache_lock:
                        cache[cid] = res
                        done_cnt += 1
                        if done_cnt % 25 == 0:
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                            print(f"  Размечено категорий: {done_cnt}/{len(todo)} ...")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    updated = 0
    for idx, row in df.iterrows():
        cid = str(row["category_id"])
        if not bool(row["classify_confident"]) and cid in cache:
            df.at[idx, "product_class"] = cache[cid]["product_class"]
            df.at[idx, "product_type"] = cache[cid]["product_type"]
            df.at[idx, "classify_confident"] = True
            updated += 1

    df.to_parquet(args.families, index=False)
    print(f"Готово! Обновлено семейств через classify_llm: {updated}. Кэш: {cache_path}")


if __name__ == "__main__":
    main()