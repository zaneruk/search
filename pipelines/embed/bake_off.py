#!/usr/bin/env python3
"""
Быстрый bake-off эмбеддинг-моделей на подвыборке families.parquet.
Сравнивает качество (Hit@1, Hit@4, MRR@10, nDCG@10) и скорость кодирования.
Автономен: можно запускать отдельно в Google Colab / Kaggle на GPU.

Пример:
  python pipelines/embed/bake_off.py \
      --families data/interim/families.parquet \
      --sample-size 2000 \
      --models BAAI/bge-m3 deepvk/USER-bge-m3 \
      --out data/interim/bake_off_report.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer


RU_TO_EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def safe_parse_json_list(val) -> List[str]:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [p.strip() for p in s.split(",") if p.strip()]
    return []


def build_family_text(row: pd.Series) -> str:
    """Единая формула текста карточки из I6: canonical_name + product_type + use_cases + category_path."""
    parts: List[str] = []
    name = str(row.get("canonical_name") or "").strip()
    vendor = str(row.get("vendor") or "").strip()
    ptype = str(row.get("product_type") or "").strip()
    cat_path = str(row.get("category_path") or "").strip()
    use_cases = safe_parse_json_list(row.get("use_cases_json") or row.get("use_cases"))

    if vendor and vendor.lower() not in name.lower():
        parts.append(f"{vendor} {name}".strip())
    elif name:
        parts.append(name)

    if ptype:
        parts.append(f"Тип: {ptype}")
    if use_cases:
        parts.append("Задачи: " + "; ".join(use_cases[:4]))
    if cat_path:
        parts.append(f"Категория: {cat_path}")
    return " | ".join([p for p in parts if p])


def translit_ru_to_en(text: str) -> str:
    out = []
    for ch in text.lower():
        out.append(RU_TO_EN.get(ch, ch))
    return "".join(out)


def make_probe_queries(df: pd.DataFrame, max_queries: int, seed: int) -> List[Tuple[str, int]]:
    """Генерирует тестовые пары (query, target_row_idx) без утечек из разных шаблонов."""
    rng = random.Random(seed)
    indices = list(range(len(df)))
    rng.shuffle(indices)

    probes: List[Tuple[str, int]] = []
    for idx in indices:
        if len(probes) >= max_queries:
            break
        row = df.iloc[idx]
        name = str(row.get("canonical_name") or "").strip()
        vendor = str(row.get("vendor") or "").strip()
        ptype = str(row.get("product_type") or "").strip()
        aliases = safe_parse_json_list(row.get("aliases_json") or row.get("aliases"))
        typical = safe_parse_json_list(row.get("typical_queries_json") or row.get("typical_queries"))

        if not name:
            continue

        mode = len(probes) % 5
        q = ""
        if mode == 0 and typical:
            q = rng.choice(typical)
        elif mode == 1 and aliases:
            q = rng.choice(aliases)
        elif mode == 2:
            # Урезанное имя без вендора + тип
            clean = re.sub(r"\([^)]*\)", "", name).strip()
            words = clean.split()
            q = " ".join(words[:3]) if len(words) > 3 else clean
            if ptype and rng.random() < 0.5:
                q = f"{ptype} {q}".strip()
        elif mode == 3 and vendor:
            # Бренд + ключевое слово названия (имитация краткого поиска)
            words = [w for w in name.split() if w.lower() != vendor.lower() and len(w) > 2]
            if words:
                q = f"{vendor} {words[0]}"
        else:
            # Транслит / смешанный регистр для проверки RU<->EN устойчивости
            words = name.split()[:3]
            candidate = " ".join(words)
            if re.search(r"[а-яА-ЯёЁ]", candidate):
                q = translit_ru_to_en(candidate)
            else:
                q = candidate.lower()

        q = " ".join(q.split())
        if len(q) >= 3:
            probes.append((q, idx))

    return probes


def evaluate_model(
    model_name: str,
    corpus_texts: List[str],
    probes: List[Tuple[str, int]],
    device: str,
    batch_size: int,
    query_prefix: str = "",
    passage_prefix: str = "",
) -> Dict:
    print(f"\n=== Тестируем модель: {model_name} (device={device}) ===")
    t_load0 = time.perf_counter()
    model = SentenceTransformer(model_name, device=device)
    load_sec = time.perf_counter() - t_load0

    passages = [f"{passage_prefix}{t}" for t in corpus_texts] if passage_prefix else corpus_texts
    t_enc0 = time.perf_counter()
    doc_emb = model.encode(
        passages,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype(np.float32)
    doc_sec = time.perf_counter() - t_enc0

    queries_txt = [f"{query_prefix}{q}" for q, _ in probes] if query_prefix else [q for q, _ in probes]
    t_q0 = time.perf_counter()
    q_emb = model.encode(
        queries_txt,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    ).astype(np.float32)
    q_sec = time.perf_counter() - t_q0

    # Косинусное сходство (векторы уже L2-нормированы)
    sims = q_emb @ doc_emb.T  # (n_queries, n_docs)

    hit1 = 0
    hit4 = 0
    mrr10 = 0.0
    ndcg10 = 0.0
    idcg10 = 1.0  # 1 релевантный документ на запрос -> max DCG = (2^1 - 1)/log2(2) = 1.0

    for i, (_, target_idx) in enumerate(probes):
        top10 = np.argpartition(-sims[i], kth=min(10, sims.shape[1] - 1))[:10]
        top10 = top10[np.argsort(-sims[i, top10])]
        ranks = np.where(top10 == target_idx)[0]
        if len(ranks) > 0:
            r = int(ranks[0]) + 1
            if r == 1:
                hit1 += 1
            if r <= 4:
                hit4 += 1
            mrr10 += 1.0 / r
            ndcg10 += 1.0 / math.log2(r + 1) / idcg10

    n = max(1, len(probes))
    result = {
        "model": model_name,
        "dim": int(doc_emb.shape[1]),
        "n_docs": len(corpus_texts),
        "n_queries": len(probes),
        "load_sec": round(load_sec, 2),
        "docs_per_sec": round(len(corpus_texts) / max(1e-6, doc_sec), 1),
        "query_ms_avg_batch": round((q_sec / n) * 1000.0, 2),
        "Hit@1": round(hit1 / n, 4),
        "Hit@4": round(hit4 / n, 4),
        "MRR@10": round(mrr10 / n, 4),
        "nDCG@10": round(ndcg10 / n, 4),
    }

    # Дополнительно замеряем одиночный запрос на CPU (как будет в проде на VPS)
    if device != "cpu":
        del model
        torch.cuda.empty_cache()
        cpu_model = SentenceTransformer(model_name, device="cpu")
    else:
        cpu_model = model

    sample_q = [q for q, _ in probes[:20]]
    cpu_latencies = []
    for q in sample_q:
        t0 = time.perf_counter()
        _ = cpu_model.encode([f"{query_prefix}{q}"], normalize_embeddings=True, show_progress_bar=False)
        cpu_latencies.append((time.perf_counter() - t0) * 1000.0)
    result["cpu_single_query_p50_ms"] = round(float(np.percentile(cpu_latencies, 50)), 1)
    result["cpu_single_query_p95_ms"] = round(float(np.percentile(cpu_latencies, 95)), 1)

    del cpu_model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Bake-off сравнение моделей эмбеддингов")
    parser.add_argument("--families", required=True, help="Путь к families.parquet")
    parser.add_argument("--sample-size", type=int, default=2000, help="Сколько family взять в подвыборку")
    parser.add_argument("--n-queries", type=int, default=300, help="Число проверочных запросов")
    parser.add_argument(
        "--models",
        nargs="+",
        default=["BAAI/bge-m3", "deepvk/USER-bge-m3"],
        help="Список HuggingFace моделей",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="data/interim/bake_off_report.json")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    df = pd.read_parquet(args.families)
    if len(df) > args.sample_size:
        # Приоритизируем доступные и популярные, но берём стратифицированно
        df = df.sort_values(by=["available_any", "pop_prior"], ascending=[False, False])
        top_half = df.iloc[: args.sample_size // 2]
        rest = df.iloc[args.sample_size // 2 :].sample(
            n=args.sample_size - len(top_half), random_state=args.seed
        )
        df = pd.concat([top_half, rest], ignore_index=True)
    else:
        df = df.reset_index(drop=True)

    corpus_texts = [build_family_text(row) for _, row in df.iterrows()]
    probes = make_probe_queries(df, max_queries=args.n_queries, seed=args.seed)
    print(f"Подготовлено {len(corpus_texts)} семейств и {len(probes)} тестовых запросов.")

    results = []
    for m in args.models:
        q_pref = "query: " if "e5" in m.lower() else ""
        p_pref = "passage: " if "e5" in m.lower() else ""
        res = evaluate_model(
            model_name=m,
            corpus_texts=corpus_texts,
            probes=probes,
            device=device,
            batch_size=args.batch_size,
            query_prefix=q_pref,
            passage_prefix=p_pref,
        )
        results.append(res)

    results_sorted = sorted(results, key=lambda r: (r["MRR@10"], r["Hit@4"], -r["cpu_single_query_p95_ms"]), reverse=True)
    winner = results_sorted[0]["model"]

    report = {"winner": winner, "device_for_docs": device, "results": results_sorted}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== ИТОГИ BAKE-OFF ===")
    print(pd.DataFrame(results_sorted).to_string(index=False))
    print(f"\nРекомендуемый победитель: {winner}")
    print(f"Отчёт сохранён в: {out_path}")


if __name__ == "__main__":
    main()