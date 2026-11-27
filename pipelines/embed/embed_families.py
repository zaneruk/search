#!/usr/bin/env python3
"""
Расчёт L2-нормированных эмбеддингов float32 (embeddings.npy) в точном порядке строк families.parquet.
Поддерживает работу как локально, так и в Google Colab / Kaggle на GPU, с чекпоинтами и авто-продолжением.

Пример (Colab / локально):
  python pipelines/embed/embed_families.py \
      --families data/interim/families.parquet \
      --model BAAI/bge-m3 \
      --out data/interim/embeddings.npy \
      --batch-size 128 \
      --checkpoint-every 2000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer


def safe_parse_list(val) -> List[str]:
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
        return [x.strip() for x in s.split(",") if x.strip()]
    return []


def format_family_text(row: pd.Series, passage_prefix: str = "") -> str:
    """Формирование чистого текста карточки строго по I6:
    canonical_name + product_type + use_cases + category_path (без сырого шумного description).
    """
    parts: List[str] = []

    name = str(row.get("canonical_name") or "").strip()
    name_ru = str(row.get("canonical_name_ru") or "").strip()
    name_en = str(row.get("canonical_name_en") or "").strip()
    vendor = str(row.get("vendor") or "").strip()
    ptype = str(row.get("product_type") or "").strip()
    cat_path = str(row.get("category_path") or "").strip()
    use_cases = safe_parse_list(row.get("use_cases_json") or row.get("use_cases"))

    # Основное имя + двуязычное уточнение от enrich (если есть)
    title_block = name
    if vendor and vendor.lower() not in name.lower():
        title_block = f"{vendor} {name}".strip()
    extra_names = [n for n in [name_ru, name_en] if n and n.lower() not in title_block.lower()]
    if extra_names:
        title_block = f"{title_block} ({', '.join(extra_names)})"

    if title_block:
        parts.append(title_block)
    if ptype:
        parts.append(f"Тип: {ptype}")
    if use_cases:
        parts.append("Задачи: " + "; ".join(use_cases[:4]))
    if cat_path:
        parts.append(f"Категория: {cat_path}")

    text = " | ".join(parts) if parts else "программное обеспечение"
    return f"{passage_prefix}{text}" if passage_prefix else text


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return (mat / norms).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация embeddings.npy для families.parquet")
    parser.add_argument("--families", required=True, help="Путь к families.parquet")
    parser.add_argument("--model", default="BAAI/bge-m3", help="Имя модели HuggingFace")
    parser.add_argument("--out", default="data/interim/embeddings.npy", help="Куда сохранить embeddings.npy")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--max-seq-length", type=int, default=256)
    parser.add_argument("--checkpoint-every", type=int, default=2048, help="Сохранять чекпоинт каждые N строк")
    parser.add_argument("--fp16", action="store_true", help="Использовать fp16 на GPU (ускоряет Colab T4 в ~2 раза)")
    parser.add_argument("--force", action="store_true", help="Игнорировать старые чекпоинты и считать заново")
    args = parser.parse_args()

    families_path = Path(args.families)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out_path.with_suffix(".meta.json")
    ckpt_npy = out_path.with_suffix(".ckpt.npy")
    ckpt_state = out_path.with_suffix(".ckpt.json")

    print(f"Читаем {families_path} ...")
    df = pd.read_parquet(families_path)
    n_rows = len(df)
    fam_sha = file_sha256(families_path)
    print(f"Строк в families.parquet: {n_rows} (sha256={fam_sha[:12]})")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Загружаем модель {args.model} на {device} ...")
    model_kwargs = {"torch_dtype": torch.float16} if (args.fp16 and device == "cuda") else {}
    model = SentenceTransformer(args.model, device=device, model_kwargs=model_kwargs)
    model.max_seq_length = args.max_seq_length
    dim = model.get_sentence_embedding_dimension()
    print(f"Размерность эмбеддингов: {dim}")

    passage_prefix = "passage: " if "e5" in args.model.lower() else ""

    # Подготовка текстов в строгом порядке строк
    texts = [format_family_text(row, passage_prefix=passage_prefix) for _, row in df.iterrows()]

    start_idx = 0
    if not args.force and ckpt_npy.exists() and ckpt_state.exists():
        try:
            state = json.loads(ckpt_state.read_text(encoding="utf-8"))
            if (
                state.get("families_sha256") == fam_sha
                and state.get("model") == args.model
                and state.get("n_rows") == n_rows
                and state.get("dim") == dim
            ):
                start_idx = int(state.get("done", 0))
                print(f"Найден валидный чекпоинт! Продолжаем со строки {start_idx} / {n_rows}")
        except Exception:
            start_idx = 0

    if start_idx == 0:
        # Создаём memmap-файл на диске, чтобы не держать дублирующую матрицу в RAM
        mm = np.lib.format.open_memmap(
            ckpt_npy, mode="w+", dtype=np.float32, shape=(n_rows, dim)
        )
        del mm

    mm = np.lib.format.open_memmap(ckpt_npy, mode="r+", dtype=np.float32, shape=(n_rows, dim))

    t0 = time.perf_counter()
    chunk_size = max(args.batch_size, args.checkpoint_every)
    for pos in range(start_idx, n_rows, chunk_size):
        end = min(pos + chunk_size, n_rows)
        batch_texts = texts[pos:end]
        embs = model.encode(
            batch_texts,
            batch_size=args.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        mm[pos:end] = l2_normalize(embs)
        mm.flush()

        ckpt_state.write_text(
            json.dumps(
                {
                    "families_sha256": fam_sha,
                    "model": args.model,
                    "n_rows": n_rows,
                    "dim": dim,
                    "done": end,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        elapsed = time.perf_counter() - t0
        speed = (end - start_idx) / max(1e-6, elapsed)
        eta_min = ((n_rows - end) / max(1e-6, speed)) / 60.0
        print(f"  [{end}/{n_rows}] ({end * 100.0 / n_rows:.1f}%) | {speed:.1f} rows/s | ETA: {eta_min:.1f} мин")

    del mm
    # Атомарное переименование готового файла в embeddings.npy
    if out_path.exists():
        out_path.unlink()
    ckpt_npy.rename(out_path)
    if ckpt_state.exists():
        ckpt_state.unlink()

    # Сохраняем метаданные, чтобы build.py проверил совместимость
    meta = {
        "model": args.model,
        "dim": dim,
        "n_rows": n_rows,
        "dtype": "float32",
        "l2_normalized": True,
        "families_sha256": fam_sha,
        "total_sec": round(time.perf_counter() - t0, 2),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово! Сохранено {out_path} ({n_rows} x {dim}) и {meta_path}")


if __name__ == "__main__":
    main()