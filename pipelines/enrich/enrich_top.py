#!/usr/bin/env python3
"""
Офлайн LLM-обогащение ТОЛЬКО топ-500–1000 популярных family (I5):
Использует Yandex AI Studio (роль strong = aliceai-llm) через gateway.py,
пишет затраты в SQLite cost_ledger, сохраняет хэш-кэш и проверяет алиасы от галлюцинаций.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yaml
from pydantic import BaseModel, Field

from services.api.llm.gateway import call_llm_structured, get_llm_credentials

try:
    from services.api.app.schemas.llm import EnrichFamilyOutput  # type: ignore
except ImportError:
    class EnrichFamilyOutput(BaseModel):
        canonical_name_ru: str
        canonical_name_en: str
        aliases: List[str] = Field(default_factory=list, max_length=12)
        typical_queries: List[str] = Field(default_factory=list, max_length=6)
        use_cases: List[str] = Field(default_factory=list, max_length=4)
        buyer_roles: List[str] = Field(default_factory=list, max_length=4)
        analogs: List[str] = Field(default_factory=list, max_length=5)
        complements: List[str] = Field(default_factory=list, max_length=5)


RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def norm_text(s: str) -> str:
    return " ".join(str(s or "").lower().replace("ё", "е").split())


def translit_ru_lat(s: str) -> str:
    return "".join(RU_TO_LAT.get(ch, ch) for ch in norm_text(s))


def load_seed_aliases(vendors_seed_path: Path, glossary_path: Path) -> Set[str]:
    known: Set[str] = set()
    for p in (vendors_seed_path, glossary_path):
        if not p.exists():
            continue
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, str):
                        known.add(norm_text(k))
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, str):
                        known.add(norm_text(item))
                    else:
                        walk(item)

        walk(data)
    return {x for x in known if len(x) >= 2}


def bigrams(s: str) -> Set[str]:
    s = re.sub(r"[^a-zа-я0-9]", "", norm_text(s))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def dice_similarity(a: str, b: str) -> float:
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2.0 * len(ba & bb) / (len(ba) + len(bb))


def filter_aliases_hallucination_guard(
    proposed_aliases: List[str],
    canonical_name: str,
    vendor: str,
    seed_known: Set[str],
) -> Tuple[List[str], List[str]]:
    auto_list: List[str] = []
    prop_list: List[str] = []

    ref_strings = [
        norm_text(canonical_name),
        norm_text(vendor),
        translit_ru_lat(canonical_name),
        translit_ru_lat(vendor),
    ]
    ref_tokens = {t for ref in ref_strings for t in ref.split() if len(t) >= 3}

    seen: Set[str] = set()
    for raw_al in proposed_aliases:
        al = " ".join(str(raw_al or "").split())
        al_n = norm_text(al)
        if not al_n or len(al_n) < 2 or al_n in seen:
            continue
        seen.add(al_n)

        al_tr = translit_ru_lat(al_n)
        in_seed = al_n in seed_known
        substr_hit = any((al_n in ref or al_tr in ref) for ref in ref_strings if ref)
        token_hit = any((tok in al_n or tok in al_tr) for tok in ref_tokens)
        fuzzy_hit = max(
            [dice_similarity(al_n, ref) for ref in ref_strings]
            + [dice_similarity(al_tr, ref) for ref in ref_strings]
            + [0.0]
        ) >= 0.42

        if in_seed or substr_hit or token_hit or fuzzy_hit:
            auto_list.append(al)
        else:
            prop_list.append(al)

    return auto_list[:10], prop_list[:6]


def deterministic_fallback_enrich(row: pd.Series) -> EnrichFamilyOutput:
    cname = str(row.get("canonical_name") or "").strip()
    ptype = str(row.get("product_type") or "").strip()
    pclass = str(row.get("product_class") or "software")

    aliases: List[str] = []
    clean_short = re.sub(r"\([^)]*\)", "", cname).strip()
    if clean_short and clean_short != cname:
        aliases.append(clean_short)
    if "-" in clean_short:
        aliases.append(clean_short.replace("-", " "))
        aliases.append(clean_short.replace("-", ""))

    use_cases = [f"{ptype}: решение для корпоративных задач"] if ptype else ["Корпоративное ИТ-решение"]
    roles = ["Системный администратор", "ИТ-закупщик"] if pclass == "hardware" else ["ИТ-специалист", "Руководитель отдела"]

    return EnrichFamilyOutput(
        canonical_name_ru=clean_short or cname,
        canonical_name_en=clean_short or cname,
        aliases=aliases[:6],
        typical_queries=[clean_short] if clean_short else [],
        use_cases=use_cases,
        buyer_roles=roles,
        analogs=[],
        complements=[],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Обогащение топ-500–1000 family через Yandex AI Studio")
    parser.add_argument("--families", default="data/interim/families.parquet")
    parser.add_argument("--vendors-seed", default="config/vendors_seed.yaml")
    parser.add_argument("--glossary", default="config/seed_glossary.yaml")
    parser.add_argument("--cache", default="data/interim/cache_enrich_family.json")
    parser.add_argument("--top-n", type=int, default=500, help="Сколько популярных family обогатить")
    parser.add_argument("--workers", type=int, default=3, help="Параллельных потоков")
    parser.add_argument("--no-llm", action="store_true", help="Не вызывать LLM (только детерминированный fallback)")
    args = parser.parse_args()

    df = pd.read_parquet(args.families)
    seed_known = load_seed_aliases(Path(args.vendors_seed), Path(args.glossary))

    vendors_data = {}
    if Path(args.vendors_seed).exists():
        vendors_data = (yaml.safe_load(Path(args.vendors_seed).read_text(encoding="utf-8")) or {}).get("vendors", {})
    key_vendors = {norm_text(k) for k in vendors_data.keys()}

    df["_is_key_vendor"] = df["vendor"].fillna("").map(lambda v: norm_text(v) in key_vendors)
    ranked_idx = (
        df.sort_values(
            by=["available_any", "_is_key_vendor", "pop_prior", "n_variants"],
            ascending=[False, False, False, False],
        )
        .head(args.top_n)
        .index
    )
    target_set = set(ranked_idx.tolist())
    print(f"Выбрано для обогащения: {len(target_set)} из {len(df)} семейств.")

    cache_path = Path(args.cache)
    cache: Dict[str, Dict[str, Any]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )

    _, _, api_key, _ = get_llm_credentials()
    use_llm = (not args.no_llm) and bool(api_key)

    prompt_path = Path("prompts/enrich_family/v1.md")
    system_prompt = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.exists()
        else "Return strict JSON with canonical_name_ru, canonical_name_en, aliases, typical_queries, use_cases, buyer_roles, analogs, complements."
    )

    if not use_llm:
        print("LLM отключен (--no-llm или нет ключа): применяю детерминированное обогащение.")

    # Готовим задания, которых ещё нет в кэше
    tasks: List[Tuple[int, str, pd.Series]] = []
    row_keys: Dict[int, str] = {}
    cache_hits = 0

    for idx in ranked_idx:
        row = df.loc[idx]
        cname = str(row.get("canonical_name") or "")
        vendor = str(row.get("vendor") or "")
        content_key = hashlib.sha256(
            f"{row['family_id']}|{cname}|{vendor}|{row.get('product_type')}".encode("utf-8")
        ).hexdigest()[:16]
        row_keys[idx] = content_key
        if content_key in cache:
            cache_hits += 1
        elif use_llm:
            tasks.append((idx, content_key, row))

    print(f"В кэше: {cache_hits}, новых LLM-запросов: {len(tasks)}")
    cache_lock = threading.Lock()
    llm_hits = 0

    def enrich_one(item: Tuple[int, str, pd.Series]) -> Tuple[str, Optional[Dict[str, Any]]]:
        _, ckey, row = item
        desc_short = str(row.get("description") or "")[:300]
        params_short = str(row.get("params_text") or "")[:350]
        user_content = (
            "<user_input>\n"
            f"name: {row.get('canonical_name')}\n"
            f"vendor: {row.get('vendor')}\n"
            f"product_class: {row.get('product_class')}\n"
            f"product_type: {row.get('product_type')}\n"
            f"category_path: {row.get('category_path')}\n"
            f"params: {params_short}\n"
            f"description: {desc_short}\n"
            "</user_input>"
        )
        parsed, _ = call_llm_structured(
            role="strong",
            purpose="enrich_family",
            system_prompt=system_prompt,
            user_payload=user_content,
            schema_cls=EnrichFamilyOutput,
            temperature=0.1,
            max_tokens_override=650,
        )
        return ckey, parsed

    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(enrich_one, t) for t in tasks]
            for fut in as_completed(futures):
                ckey, parsed = fut.result()
                if parsed is not None:
                    with cache_lock:
                        cache[ckey] = parsed
                        llm_hits += 1
                        if llm_hits % 25 == 0:
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                            print(f"  Обогащено LLM: {llm_hits}/{len(tasks)} ...")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    # Записываем итоговые колонки в строгом порядке строк df
    col_ru, col_en = [], []
    col_al, col_prop_al = [], []
    col_tq, col_uc, col_roles = [], [], []
    col_analogs, col_comp = [], []

    for idx, row in df.iterrows():
        cname = str(row.get("canonical_name") or "")
        vendor = str(row.get("vendor") or "")
        enrich_obj: Optional[EnrichFamilyOutput] = None

        if idx in target_set:
            ckey = row_keys[idx]
            if ckey in cache:
                try:
                    enrich_obj = EnrichFamilyOutput.model_validate(cache[ckey])
                except Exception:
                    enrich_obj = None

        if enrich_obj is None:
            enrich_obj = deterministic_fallback_enrich(row)

        auto_als, prop_als = filter_aliases_hallucination_guard(
            enrich_obj.aliases, cname, vendor, seed_known
        )
        col_ru.append(enrich_obj.canonical_name_ru)
        col_en.append(enrich_obj.canonical_name_en)
        col_al.append(json.dumps(auto_als, ensure_ascii=False))
        col_prop_al.append(json.dumps(prop_als, ensure_ascii=False))
        col_tq.append(json.dumps(enrich_obj.typical_queries, ensure_ascii=False))
        col_uc.append(json.dumps(enrich_obj.use_cases, ensure_ascii=False))
        col_roles.append(json.dumps(enrich_obj.buyer_roles, ensure_ascii=False))
        col_analogs.append(json.dumps(enrich_obj.analogs, ensure_ascii=False))
        col_comp.append(json.dumps(enrich_obj.complements, ensure_ascii=False))

    df = df.drop(columns=["_is_key_vendor"], errors="ignore")
    df["canonical_name_ru"] = col_ru
    df["canonical_name_en"] = col_en
    df["aliases_json"] = col_al
    df["proposed_aliases_json"] = col_prop_al
    df["typical_queries_json"] = col_tq
    df["use_cases_json"] = col_uc
    df["buyer_roles_json"] = col_roles
    df["analogs_json"] = col_analogs
    df["complements_json"] = col_comp

    df.to_parquet(args.families, index=False)
    print(f"Готово! Обновлён {args.families} (cache_hits={cache_hits}, llm_new={llm_hits}).")


if __name__ == "__main__":
    main()