#!/usr/bin/env python3
"""
Финальная сборка иммутабельного индекса data/artifacts/<index_version>/ строго по CONTRACTS.md (B.5)
и smart_search_architecture (1).md (п. 2.1, 2.2 I6, 2.5).

Создаёт:
  - families.parquet, offers.parquet, data_quality_issues.parquet, qc_report.json
  - entities.parquet (BRAND, FAMILY, CATEGORY, CONTENT, EXTERNAL_PRODUCT)
  - graph_edges.parquet (vendor_evidence, catalog_structure, llm_validated)
  - embeddings.npy (верифицированный, L2-нормированный float32 в порядке families.parquet)
  - bm25f.npz + bm25f_vocab.pkl
  - char3gram.npz + char3gram_vocab.pkl
  - symspell.pkl
  - typeahead_trie.pkl
  - popular_by_root.json
  - manifest.json + атомарный указатель data/artifacts/current

Пример запуска:
  python pipelines/build_index/build.py \
      --interim-dir data/interim \
      --raw-feed data/raw/feed.yml \
      --embeddings data/interim/embeddings.npy \
      --artifacts-root data/artifacts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import shutil
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
import pymorphy3
import scipy.sparse as sp
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from symspellpy import SymSpell

from pipelines.build_index.manifest import (
    make_index_version,
    sha256_file,
    switch_current_pointer,
    write_manifest,
)

# ---------------------------------------------------------------------------
# Нормализация, лемматизация и разделение name_head / name_target
# ---------------------------------------------------------------------------

_MORPH = pymorphy3.MorphAnalyzer()

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+(?:[-.][a-zа-яё0-9]+)*", re.IGNORECASE)
_CYR_RE = re.compile(r"[а-яё]")
_HEAD_SPLIT_RE = re.compile(r"\s+(?:для|на|под|к|с|for|on|under)\s+", re.IGNORECASE)

RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def norm_str(text: Any) -> str:
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return ""
    s = str(text).strip().lower().replace("ё", "е")
    return " ".join(s.split())


def slugify(text: str) -> str:
    s = norm_str(text)
    out = []
    for ch in s:
        if ch in RU_TO_LAT:
            out.append(RU_TO_LAT[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "/", "."):
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    if not slug:
        slug = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    return slug[:64]


@lru_cache(maxsize=250_000)
def lemmatize_token(token: str) -> str:
    t = token.lower().replace("ё", "е")
    if _CYR_RE.search(t) and len(t) >= 2:
        return _MORPH.parse(t)[0].normal_form.replace("ё", "е")
    return t


def tokenize_lemmas(text: Any) -> List[str]:
    s = norm_str(text)
    if not s:
        return []
    tokens: List[str] = []
    for raw in _TOKEN_RE.findall(s):
        # Дефисные слова сохраняем и целиком (сканер-вс), и частями (сканер, вс), и слитно (сканервс)
        if "-" in raw:
            tokens.append(lemmatize_token(raw))
            joined = raw.replace("-", "")
            if joined:
                tokens.append(lemmatize_token(joined))
            for part in raw.split("-"):
                if part:
                    tokens.append(lemmatize_token(part))
        else:
            tokens.append(lemmatize_token(raw))
    return tokens


def split_name_head_target(name: str) -> Tuple[str, str]:
    """Разделение названия (I6): часть ДО 'для/на/под' — это ЧТО это (name_head),
    часть ПОСЛЕ — для чего оно (name_target).
    """
    s = " ".join(str(name or "").split())
    if not s:
        return "", ""
    m = _HEAD_SPLIT_RE.search(s)
    if not m:
        return s, ""
    head = s[: m.start()].strip()
    target = s[m.start() :].strip()
    return (head or s), target


def parse_json_list(val: Any) -> List[str]:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return []
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        return [p.strip() for p in s.split(",") if p.strip()]
    return []


def load_yaml_safe(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 1. Сбор entities.parquet (BRAND, FAMILY, CATEGORY, CONTENT, EXTERNAL_PRODUCT)
# ---------------------------------------------------------------------------

def build_entities(
    families_df: pd.DataFrame,
    offers_df: pd.DataFrame,
    categories_df: Optional[pd.DataFrame],
    seed_glossary: Dict[str, Any],
    vendors_seed: Dict[str, Any],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    used_ids: Set[str] = set()

    def unique_entity_id(kind: str, raw_key: str) -> str:
        base = f"e_{kind.lower()}_{slugify(raw_key)}"
        eid = base
        if eid in used_ids:
            suffix = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:6]
            eid = f"{base[:56]}_{suffix}"
        used_ids.add(eid)
        return eid

    # Извлекаем справочник вендорных алиасов из vendors_seed.yaml и seed_glossary.yaml
    vendor_seed_aliases: Dict[str, Set[str]] = defaultdict(set)
    vendors_cfg = vendors_seed.get("vendors", vendors_seed)
    if isinstance(vendors_cfg, dict):
        for v_name, v_data in vendors_cfg.items():
            if not isinstance(v_data, dict):
                continue
            key = norm_str(v_name)
            for al in parse_json_list(v_data.get("aliases")):
                vendor_seed_aliases[key].add(al)

    brand_glossary = seed_glossary.get("brands", {}) or seed_glossary.get("brand_aliases", {})
    if isinstance(brand_glossary, dict):
        for b_name, als in brand_glossary.items():
            for al in parse_json_list(als):
                vendor_seed_aliases[norm_str(b_name)].add(al)

    # 1.1 BRAND
    if "vendor" in families_df.columns:
        for vendor, grp in families_df.groupby("vendor", dropna=True):
            v_clean = str(vendor).strip()
            if not v_clean or v_clean.lower() in ("none", "nan", "не определено"):
                continue
            v_key = norm_str(v_clean)
            aliases = {v_clean, v_key}
            aliases.update(vendor_seed_aliases.get(v_key, set()))
            # Добавляем базовый транслит/слитное написание
            if " " in v_key:
                aliases.add(v_key.replace(" ", ""))
            fam_ids = grp["family_id"].astype(str).tolist()
            v_url = None
            if isinstance(vendors_cfg, dict):
                for vn, vd in vendors_cfg.items():
                    if norm_str(vn) == v_key and isinstance(vd, dict):
                        v_url = vd.get("domain") or vd.get("url") or vd.get("evidence_url")
                        break
            rows.append(
                {
                    "entity_id": unique_entity_id("brand", v_clean),
                    "kind": "BRAND",
                    "canonical_name": v_clean,
                    "aliases_json": json.dumps(sorted(aliases), ensure_ascii=False),
                    "status": "verified",
                    "target_family_ids_json": json.dumps(fam_ids, ensure_ascii=False),
                    "url": v_url,
                    "evidence_snippet": None,
                }
            )

    # 1.2 FAMILY
    # Индексируем числовые/сленговые алиасы из seed_glossary, если подходят по имени
    slang_map = seed_glossary.get("synonyms", {}) or seed_glossary.get("slang", {})
    numeric_aliases = seed_glossary.get("numeric_aliases", {})
    split_join_cfg = seed_glossary.get("aliases", {})

    for _, r in families_df.iterrows():
        fid = str(r["family_id"])
        cname = str(r.get("canonical_name") or "").strip()
        if not cname:
            continue
        aliases: Set[str] = {cname, norm_str(cname)}
        for extra_col in ("canonical_name_ru", "canonical_name_en"):
            val = str(r.get(extra_col) or "").strip()
            if val and val.lower() != "nan":
                aliases.add(val)

        # Алиасы от обогащения (2.2)
        enriched_als = parse_json_list(r.get("aliases_json") or r.get("aliases"))
        aliases.update(enriched_als)

        # Авто-варианты дефисов/пробелов для устойчивости L1
        c_norm = norm_str(cname)
        if "-" in c_norm:
            aliases.add(c_norm.replace("-", " "))
            aliases.add(c_norm.replace("-", ""))

        # Подтягиваем числовые алиасы (напр., "365" -> "microsoft 365")
        if isinstance(numeric_aliases, dict):
            for num_k, target_phrase in numeric_aliases.items():
                if norm_str(target_phrase) in c_norm:
                    aliases.add(str(num_k))

        if isinstance(split_join_cfg, dict):
            for canon_k, als in split_join_cfg.items():
                if norm_str(canon_k) in c_norm:
                    aliases.update(parse_json_list(als))

        # Статус: если есть verified/seed или совпадает с каталогом — auto/verified
        status = "auto" if enriched_als else "verified"
        url = None
        if "url" in r and pd.notna(r["url"]):
            url = str(r["url"])

        rows.append(
            {
                "entity_id": unique_entity_id("family", fid),
                "kind": "FAMILY",
                "canonical_name": cname,
                "aliases_json": json.dumps(sorted(a for a in aliases if a), ensure_ascii=False),
                "status": status,
                "target_family_ids_json": json.dumps([fid], ensure_ascii=False),
                "url": url,
                "evidence_snippet": None,
            }
        )

    # 1.3 CATEGORY
    cat_to_fams: Dict[str, List[str]] = defaultdict(list)
    for _, r in families_df.iterrows():
        cid = str(r.get("category_id") or "").strip()
        if cid:
            cat_to_fams[cid].append(str(r["family_id"]))

    if categories_df is not None and len(categories_df) > 0:
        for _, cr in categories_df.iterrows():
            cid = str(cr.get("category_id") or cr.get("id") or "").strip()
            cname = str(cr.get("name") or "").strip()
            if not cid or not cname:
                continue
            cpath = str(cr.get("path") or cname).strip()
            fams = cat_to_fams.get(cid, [])
            if not fams and "path" in families_df.columns:
                # Если категория родительская — привязываем до 50 популярных family по вхождению пути
                matched = families_df[families_df["category_path"].fillna("").str.contains(re.escape(cname), case=False)]
                if len(matched) > 0:
                    fams = matched.sort_values("pop_prior", ascending=False)["family_id"].head(50).astype(str).tolist()
            cat_aliases = {cname, norm_str(cname)}
            if isinstance(slang_map, dict):
                for sk, sv in slang_map.items():
                    if norm_str(cname) in norm_str(sv) or any(norm_str(x) in norm_str(cname) for x in parse_json_list(sv)):
                        cat_aliases.add(str(sk))
            rows.append(
                {
                    "entity_id": unique_entity_id("category", f"{cid}_{cname}"),
                    "kind": "CATEGORY",
                    "canonical_name": cname,
                    "aliases_json": json.dumps(sorted(cat_aliases), ensure_ascii=False),
                    "status": "verified",
                    "target_family_ids_json": json.dumps(fams[:100], ensure_ascii=False),
                    "url": str(cr["url"]) if "url" in cr and pd.notna(cr["url"]) else None,
                    "evidence_snippet": cpath[:200],
                }
            )
    else:
        for cid, fam_ids in cat_to_fams.items():
            sub = families_df[families_df["category_id"].astype(str) == cid]
            cpath = str(sub.iloc[0].get("category_path") or cid)
            cname = cpath.split("/")[-1].strip() if "/" in cpath else cpath
            rows.append(
                {
                    "entity_id": unique_entity_id("category", f"{cid}_{cname}"),
                    "kind": "CATEGORY",
                    "canonical_name": cname,
                    "aliases_json": json.dumps([cname, norm_str(cname)], ensure_ascii=False),
                    "status": "verified",
                    "target_family_ids_json": json.dumps(fam_ids[:100], ensure_ascii=False),
                    "url": None,
                    "evidence_snippet": cpath[:200],
                }
            )

    # 1.4 CONTENT (разделы сайта, вебинары, vendorUrl из фида и vendors_seed.yaml)
    seen_content_urls: Set[str] = set()
    if "url" in offers_df.columns:
        # Ищем не-товарные разделы / вебинары / лендинги вендоров, если есть в URL фида
        urls = offers_df["url"].dropna().astype(str).unique().tolist()
        for u in urls:
            low = u.lower()
            if any(k in low for k in ("/webinar", "/events", "/promo", "/vendors/")):
                if u in seen_content_urls:
                    continue
                seen_content_urls.add(u)
                title = u.rstrip("/").split("/")[-1].replace("-", " ").title()
                rows.append(
                    {
                        "entity_id": unique_entity_id("content", u),
                        "kind": "CONTENT",
                        "canonical_name": title,
                        "aliases_json": json.dumps([title, norm_str(title)], ensure_ascii=False),
                        "status": "auto",
                        "target_family_ids_json": "[]",
                        "url": u,
                        "evidence_snippet": "Раздел из URL фида",
                    }
                )

    # Из vendors_seed.yaml: content_pages и external_products
    content_seed = vendors_seed.get("content_pages", [])
    if isinstance(content_seed, list):
        for cp in content_seed:
            if not isinstance(cp, dict):
                continue
            title = str(cp.get("title") or "").strip()
            url = str(cp.get("url") or "").strip()
            if not title:
                continue
            als = set(parse_json_list(cp.get("aliases"))) | {title, norm_str(title)}
            rows.append(
                {
                    "entity_id": unique_entity_id("content", f"{title}_{url}"),
                    "kind": "CONTENT",
                    "canonical_name": title,
                    "aliases_json": json.dumps(sorted(als), ensure_ascii=False),
                    "status": "seed",
                    "target_family_ids_json": json.dumps(parse_json_list(cp.get("related_family_ids")), ensure_ascii=False),
                    "url": url or None,
                    "evidence_snippet": str(cp.get("snippet") or "")[:200] or None,
                }
            )

    # 1.5 EXTERNAL_PRODUCT (из vendors_seed.yaml)
    ext_list = list(vendors_seed.get("external_products", []) or [])
    if isinstance(vendors_cfg, dict):
        for v_name, v_data in vendors_cfg.items():
            if isinstance(v_data, dict) and isinstance(v_data.get("external_products"), list):
                for ep in v_data["external_products"]:
                    if isinstance(ep, dict):
                        ep_copy = dict(ep)
                        ep_copy.setdefault("vendor", v_name)
                        ext_list.append(ep_copy)
                    elif isinstance(ep, str):
                        ext_list.append({"name": ep, "vendor": v_name, "evidence_url": v_data.get("evidence_url")})

    for ep in ext_list:
        if not isinstance(ep, dict):
            continue
        ename = str(ep.get("name") or ep.get("canonical_name") or "").strip()
        if not ename:
            continue
        als = set(parse_json_list(ep.get("aliases"))) | {ename, norm_str(ename)}
        rows.append(
            {
                "entity_id": unique_entity_id("external_product", ename),
                "kind": "EXTERNAL_PRODUCT",
                "canonical_name": ename,
                "aliases_json": json.dumps(sorted(als), ensure_ascii=False),
                "status": "seed",
                "target_family_ids_json": json.dumps(parse_json_list(ep.get("analog_family_ids")), ensure_ascii=False),
                "url": ep.get("evidence_url") or ep.get("url"),
                "evidence_snippet": str(ep.get("snippet") or f"Внешний продукт вендора {ep.get('vendor', '')}")[:200],
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Сбор graph_edges.parquet
# ---------------------------------------------------------------------------

def build_graph_edges(
    families_df: pd.DataFrame,
    taxonomy: Dict[str, Any],
    vendors_seed: Dict[str, Any],
) -> pd.DataFrame:
    """Формирует рёбра графа допродаж/аналогов строго по схеме B.5."""
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str, str, str]] = set()

    # Индекс поиска family по нормализованному имени и бренду
    fam_by_vendor: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fam_by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fam_records: List[Dict[str, Any]] = []

    fam_sorted = families_df.sort_values(
        by=["available_any", "pop_prior"], ascending=[False, False]
    )
    for _, r in fam_sorted.iterrows():
        rec = {
            "family_id": str(r["family_id"]),
            "name_norm": norm_str(r.get("canonical_name")),
            "vendor_norm": norm_str(r.get("vendor")),
            "product_type": str(r.get("product_type") or "").strip(),
            "pop_prior": float(r.get("pop_prior") or 0.0),
            "available_any": bool(r.get("available_any")),
        }
        fam_records.append(rec)
        if rec["vendor_norm"]:
            fam_by_vendor[rec["vendor_norm"]].append(rec)
        if rec["product_type"]:
            fam_by_type[rec["product_type"]].append(rec)

    def resolve_families_by_hint(hint: str, vendor_hint: str = "") -> List[str]:
        h = norm_str(hint)
        v = norm_str(vendor_hint)
        if not h:
            return []
        pool = fam_by_vendor.get(v, fam_records) if v else fam_records
        hits = [rec["family_id"] for rec in pool if h in rec["name_norm"]]
        if not hits and v:
            hits = [rec["family_id"] for rec in fam_records if h in rec["name_norm"]]
        return hits[:5]

    def add_edge(
        src_fid: str,
        dst_fid: Optional[str],
        dst_ext: Optional[str],
        edge_type: str,
        source: str,
        evidence_url: Optional[str],
        snippet: Optional[str],
        confidence: float,
    ) -> None:
        if not src_fid or (dst_fid and src_fid == dst_fid):
            return
        key = (src_fid, str(dst_fid or ""), str(dst_ext or ""), edge_type)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(
            {
                "src_family_id": src_fid,
                "dst_family_id": dst_fid,
                "dst_external_name": dst_ext,
                "edge_type": edge_type,
                "source": source,
                "evidence_url": evidence_url,
                "snippet": str(snippet)[:200] if snippet else None,
                "confidence": round(float(confidence), 4),
            }
        )

    # 2.1 Рёбра из vendors_seed.yaml (vendor_evidence)
    vendors_cfg = vendors_seed.get("vendors", vendors_seed)
    if isinstance(vendors_cfg, dict):
        for v_name, v_data in vendors_cfg.items():
            if not isinstance(v_data, dict):
                continue
            v_fams = fam_by_vendor.get(norm_str(v_name), [])
            default_url = v_data.get("evidence_url") or v_data.get("url")
            default_snip = v_data.get("snippet")

            for rel_key in ("complements", "upsell_to", "analog_of", "requires", "integrates_with"):
                rel_items = v_data.get(rel_key, [])
                if not isinstance(rel_items, list):
                    continue
                for item in rel_items:
                    if isinstance(item, dict):
                        src_hint = item.get("src") or item.get("from") or ""
                        dst_hint = item.get("dst") or item.get("to") or item.get("target") or item.get("name") or ""
                        ev_url = item.get("evidence_url") or default_url
                        snip = item.get("snippet") or default_snip or f"Рекомендация вендора {v_name}"
                        conf = float(item.get("confidence", 0.92))
                    else:
                        src_hint, dst_hint = "", str(item)
                        ev_url, snip, conf = default_url, default_snip, 0.88

                    src_ids = resolve_families_by_hint(src_hint, v_name) if src_hint else [x["family_id"] for x in v_fams[:10]]
                    dst_ids = resolve_families_by_hint(dst_hint)
                    for sid in src_ids:
                        if dst_ids:
                            for did in dst_ids[:3]:
                                add_edge(sid, did, None, rel_key, "vendor_evidence", ev_url, snip, conf)
                        elif dst_hint:
                            add_edge(sid, None, str(dst_hint), rel_key, "vendor_evidence", ev_url, snip, conf * 0.9)

    # 2.2 Матрица допустимых пар из config/taxonomy.yaml -> structural edges & валидация LLM
    allowed_pairs: Set[Tuple[str, str]] = set()
    pairs_cfg = taxonomy.get("complements_matrix") or taxonomy.get("allowed_pairs") or taxonomy.get("cross_sell_pairs") or []
    if isinstance(pairs_cfg, dict):
        for src_t, dst_list in pairs_cfg.items():
            for dst_t in parse_json_list(dst_list):
                allowed_pairs.add((str(src_t).strip(), str(dst_t).strip()))
    elif isinstance(pairs_cfg, list):
        for pair in pairs_cfg:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                allowed_pairs.add((str(pair[0]).strip(), str(pair[1]).strip()))
            elif isinstance(pair, dict) and pair.get("src") and pair.get("dst"):
                allowed_pairs.add((str(pair["src"]).strip(), str(pair["dst"]).strip()))

    # Если матрица не задана явно — безопасные дефолты по общим типам
    allowed_dst_by_src: Dict[str, List[str]] = defaultdict(list)
    for st, dt in allowed_pairs:
        allowed_dst_by_src[st].append(dt)

    # 2.3 LLM-validated кандидаты из обогащения (2.2)
    fam_type_map = {r["family_id"]: r["product_type"] for r in fam_records}
    for _, r in families_df.iterrows():
        src_fid = str(r["family_id"])
        src_type = str(r.get("product_type") or "").strip()
        for col, etype in (("complements_json", "complements"), ("analogs_json", "analog_of")):
            targets = parse_json_list(r.get(col) or r.get(col.replace("_json", "")))
            for tgt_name in targets:
                dst_ids = resolve_families_by_hint(tgt_name)
                for did in dst_ids[:2]:
                    dst_type = fam_type_map.get(did, "")
                    # Проверяем по матрице (для analog_of — тот же product_type, для complements — разрешённая пара или пустая матрица)
                    if etype == "analog_of":
                        is_ok = (not src_type or not dst_type or src_type == dst_type)
                    else:
                        is_ok = (not allowed_pairs) or ((src_type, dst_type) in allowed_pairs)
                    if is_ok:
                        add_edge(
                            src_fid,
                            did,
                            None,
                            etype,
                            "llm_validated",
                            None,
                            f"Связанный продукт: {tgt_name}",
                            0.78,
                        )

    # 2.4 Структурные рёбра (catalog_structure): внутри экосистемы вендора + по матрице типов
    for rec in fam_records:
        src_fid = rec["family_id"]
        src_type = rec["product_type"]
        v_norm = rec["vendor_norm"]

        # Сначала ищем дополняющие типы у ТОГО ЖЕ вендора (сильная экосистемная связка)
        dst_types = allowed_dst_by_src.get(src_type, [])
        added_struct = 0
        if v_norm and dst_types:
            for cand in fam_by_vendor.get(v_norm, []):
                if cand["available_any"] and cand["product_type"] in dst_types:
                    add_edge(
                        src_fid,
                        cand["family_id"],
                        None,
                        "complements",
                        "catalog_structure",
                        None,
                        f"Экосистема вендора ({cand['product_type']})",
                        0.75,
                    )
                    added_struct += 1
                    if added_struct >= 3:
                        break

        # Если у того же вендора не набралось — берём топ-2 популярных доступных из разрешённого типа
        if added_struct < 2 and dst_types:
            for dt in dst_types[:2]:
                cands = [c for c in fam_by_type.get(dt, []) if c["available_any"]]
                if cands:
                    add_edge(
                        src_fid,
                        cands[0]["family_id"],
                        None,
                        "complements",
                        "catalog_structure",
                        None,
                        f"Сопутствующий класс решения: {dt}",
                        0.65,
                    )

    if not edges:
        return pd.DataFrame(
            columns=[
                "src_family_id", "dst_family_id", "dst_external_name",
                "edge_type", "source", "evidence_url", "snippet", "confidence",
            ]
        )
    return pd.DataFrame(edges)


# ---------------------------------------------------------------------------
# 3. Разреженные матрицы BM25F и Char-3gram
# ---------------------------------------------------------------------------

def build_bm25f_index(
    families_df: pd.DataFrame,
    offers_df: pd.DataFrame,
    search_cfg: Dict[str, Any],
) -> Tuple[sp.csr_matrix, Dict[str, Any]]:
    """
    Строит единую предвзвешенную CSR-матрицу BM25F (n_families x n_vocab)
    по 7 полям I6: name_head, brand, category_path, aliases, params_values, description, name_target.
    """
    n_docs = len(families_df)
    default_weights = {
        "name_head": 3.0,
        "brand": 2.0,
        "category_path": 1.5,
        "aliases": 1.2,
        "params": 0.8,
        "description": 0.4,
        "name_target": 0.3,
    }
    cfg_weights = (search_cfg.get("bm25f", {}) or {}).get("field_weights", {})
    field_weights = {k: float(cfg_weights.get(k, v)) for k, v in default_weights.items()}
    k1 = float((search_cfg.get("bm25f", {}) or {}).get("k1", 1.2))
    b = float((search_cfg.get("bm25f", {}) or {}).get("b", 0.5))

    # Подтягиваем компактные params/description из offers по representative_offer_id (если их нет в families)
    offer_text_map: Dict[str, Tuple[str, str]] = {}
    if len(offers_df) > 0:
        desc_col = "description_clean" if "description_clean" in offers_df.columns else ("description" if "description" in offers_df.columns else None)
        param_col = "params_text" if "params_text" in offers_df.columns else None
        if desc_col or param_col:
            rep_ids = set(families_df["representative_offer_id"].dropna().astype(str).tolist())
            sub_off = offers_df[offers_df["offer_id"].astype(str).isin(rep_ids)]
            for _, orow in sub_off.iterrows():
                oid = str(orow["offer_id"])
                p_txt = str(orow[param_col]) if param_col and pd.notna(orow[param_col]) else ""
                d_txt = str(orow[desc_col]) if desc_col and pd.notna(orow[desc_col]) else ""
                offer_text_map[oid] = (p_txt[:400], d_txt[:500])

    # Собираем токены по всем полям
    fields = list(field_weights.keys())
    field_tokens: Dict[str, List[List[str]]] = {f: [] for f in fields}
    vocab_df: Counter = Counter()

    for _, r in families_df.iterrows():
        cname = str(r.get("canonical_name") or "")
        head, target = split_name_head_target(cname)
        brand = str(r.get("vendor") or "")
        cat_path = str(r.get("category_path") or "").replace("/", " ")
        als_list = parse_json_list(r.get("aliases_json") or r.get("aliases"))
        typ_q = parse_json_list(r.get("typical_queries_json") or r.get("typical_queries"))
        als_txt = " ".join(als_list + typ_q)

        rep_oid = str(r.get("representative_offer_id") or "")
        off_p, off_d = offer_text_map.get(rep_oid, ("", ""))
        params_txt = str(r.get("params_text") or off_p or "")
        desc_txt = str(r.get("description") or off_d or "")

        row_map = {
            "name_head": tokenize_lemmas(head),
            "name_target": tokenize_lemmas(target),
            "brand": tokenize_lemmas(brand),
            "category_path": tokenize_lemmas(cat_path),
            "aliases": tokenize_lemmas(als_txt),
            "params": tokenize_lemmas(params_txt),
            "description": tokenize_lemmas(desc_txt),
        }

        doc_unique_terms: Set[str] = set()
        for f in fields:
            toks = row_map[f]
            field_tokens[f].append(toks)
            doc_unique_terms.update(toks)
        for t in doc_unique_terms:
            vocab_df[t] += 1

    vocab = {term: idx for idx, term in enumerate(sorted(vocab_df.keys()))}
    n_vocab = len(vocab)
    if n_vocab == 0:
        return sp.csr_matrix((n_docs, 0), dtype=np.float32), {"vocab": {}, "idf": [], "field_weights": field_weights}

    # IDF по документам
    idf = np.zeros(n_vocab, dtype=np.float32)
    for term, idx in vocab.items():
        df_t = vocab_df[term]
        idf[idx] = math.log(1.0 + (n_docs - df_t + 0.5) / (df_t + 0.5))

    # Строим CSR матрицу pseudo_tf по каждому полю с нормализацией длины поля
    pseudo_tf = sp.csr_matrix((n_docs, n_vocab), dtype=np.float32)

    for f in fields:
        w_f = field_weights[f]
        if w_f <= 0:
            continue
        toks_list = field_tokens[f]
        lengths = np.array([len(t) for t in toks_list], dtype=np.float32)
        non_empty = lengths[lengths > 0]
        avgdl = float(non_empty.mean()) if len(non_empty) > 0 else 1.0

        indptr = [0]
        indices = []
        data = []
        for row_idx, toks in enumerate(toks_list):
            if not toks:
                indptr.append(len(indices))
                continue
            counts = Counter(toks)
            norm = (1.0 - b) + b * (lengths[row_idx] / max(1.0, avgdl))
            scale = w_f / max(1e-6, norm)
            for term, cnt in counts.items():
                col = vocab.get(term)
                if col is not None:
                    indices.append(col)
                    data.append(cnt * scale)
            indptr.append(len(indices))

        mat_f = sp.csr_matrix(
            (np.array(data, dtype=np.float32), np.array(indices, dtype=np.int32), np.array(indptr, dtype=np.int32)),
            shape=(n_docs, n_vocab),
        )
        pseudo_tf = pseudo_tf + mat_f

    # Насыщение BM25F и умножение столбцов на IDF
    pseudo_tf.sum_duplicates()
    pseudo_tf.data = pseudo_tf.data / (k1 + pseudo_tf.data)
    idf_diag = sp.diags(idf, format="csr", dtype=np.float32)
    bm25f_matrix = (pseudo_tf @ idf_diag).tocsr().astype(np.float32)

    meta = {
        "vocab": vocab,
        "idf": idf.tolist(),
        "field_weights": field_weights,
        "k1": k1,
        "b": b,
    }
    return bm25f_matrix, meta


def build_char3gram_index(families_df: pd.DataFrame) -> Tuple[sp.csr_matrix, Dict[str, Any]]:
    """Char-3gram индекс по brand + name_head + aliases (I6) для отлова опечаток и транслита."""
    corpus: List[str] = []
    for _, r in families_df.iterrows():
        head, _ = split_name_head_target(str(r.get("canonical_name") or ""))
        vendor = str(r.get("vendor") or "")
        als = " ".join(parse_json_list(r.get("aliases_json") or r.get("aliases")))
        text = norm_str(f"{vendor} {head} {als}")
        corpus.append(text if text else "empty")

    vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 3),
        min_df=2 if len(corpus) >= 50 else 1,
        dtype=np.float32,
        norm="l2",
    )
    mat = vec.fit_transform(corpus).tocsr().astype(np.float32)
    meta = {
        "vocabulary": {k: int(v) for k, v in vec.vocabulary_.items()},
        "idf": vec.idf_.astype(np.float32).tolist(),
        "ngram_range": [3, 3],
        "analyzer": "char_wb",
    }
    return mat, meta


# ---------------------------------------------------------------------------
# 4. SymSpell, Typeahead Trie и Popular by Root
# ---------------------------------------------------------------------------

def build_symspell_and_trie(
    families_df: pd.DataFrame,
    entities_df: pd.DataFrame,
    seed_glossary: Dict[str, Any],
) -> Tuple[SymSpell, Dict[str, Any]]:
    """Собирает SymSpell по леммам/брендам/алиасам и префиксный индекс для <60 мс typeahead."""
    word_freq: Counter = Counter()

    # Частоты из каталога
    for _, r in families_df.iterrows():
        pop_boost = max(1, int(round(10 * float(r.get("pop_prior") or 0.1))))
        for txt in (r.get("canonical_name"), r.get("vendor")):
            for tok in _TOKEN_RE.findall(norm_str(txt)):
                if len(tok) >= 3:
                    word_freq[tok] += pop_boost
                    word_freq[lemmatize_token(tok)] += pop_boost
        for al in parse_json_list(r.get("aliases_json") or r.get("aliases")):
            for tok in _TOKEN_RE.findall(norm_str(al)):
                if len(tok) >= 3:
                    word_freq[tok] += 5

    # Добавляем seed-глоссарий с высоким приоритетом
    for section in ("synonyms", "slang", "aliases", "brands", "brand_aliases"):
        sec = seed_glossary.get(section, {})
        if isinstance(sec, dict):
            for k, v in sec.items():
                for tok in _TOKEN_RE.findall(norm_str(k)):
                    if len(tok) >= 3:
                        word_freq[tok] += 50
                for item in parse_json_list(v):
                    for tok in _TOKEN_RE.findall(norm_str(item)):
                        if len(tok) >= 3:
                            word_freq[tok] += 25

    sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    for word, freq in word_freq.items():
        sym.create_dictionary_entry(word, int(freq))

    # --- Typeahead Trie (быстрая префиксная структура для T1 + T2) ---
    # 1) Компактный справочник family для мгновенных мини-карточек (T2)
    fam_compact: List[Dict[str, Any]] = []
    for _, r in families_df.iterrows():
        fam_compact.append(
            {
                "family_id": str(r["family_id"]),
                "canonical_name": str(r.get("canonical_name") or ""),
                "vendor": str(r.get("vendor") or ""),
                "image_url": str(r["image_url"]) if "image_url" in r and pd.notna(r["image_url"]) else None,
                "price_min_minor": int(r["price_min_minor"]) if pd.notna(r.get("price_min_minor")) else None,
                "available": bool(r.get("available_any")),
                "has_promo": bool(pd.notna(r.get("discount_best")) and float(r.get("discount_best") or 0) > 0),
                "pop_prior": float(r.get("pop_prior") or 0.0),
            }
        )

    fam_id_to_idx = {item["family_id"]: i for i, item in enumerate(fam_compact)}

    # 2) Строки-кандидаты подсказок (brand, product, alias, category, seed_query)
    completions: List[Dict[str, Any]] = []
    seen_comp: Set[Tuple[str, str]] = set()

    def add_completion(text: str, kind: str, score: float, family_indices: List[int]) -> None:
        t_clean = " ".join(str(text).split())
        key = (norm_str(t_clean), kind)
        if not t_clean or len(t_clean) < 2 or key in seen_comp:
            return
        seen_comp.add(key)
        completions.append(
            {
                "text": t_clean,
                "text_norm": norm_str(t_clean),
                "kind": kind,
                "score": round(float(score), 4),
                "family_indices": family_indices[:6],
            }
        )

    for _, er in entities_df.iterrows():
        kind = str(er["kind"])
        cname = str(er["canonical_name"])
        tfams = [fam_id_to_idx[fid] for fid in parse_json_list(er["target_family_ids_json"]) if fid in fam_id_to_idx]
        tfams.sort(key=lambda idx: (fam_compact[idx]["available"], fam_compact[idx]["pop_prior"]), reverse=True)

        if kind == "BRAND":
            add_completion(cname, "brand", 1.5 + 0.01 * len(tfams), tfams)
            for al in parse_json_list(er["aliases_json"]):
                if norm_str(al) != norm_str(cname):
                    add_completion(al, "alias", 1.2 + 0.005 * len(tfams), tfams)
        elif kind == "CATEGORY":
            add_completion(cname, "category", 1.1 + 0.005 * len(tfams), tfams)
        elif kind == "FAMILY" and tfams:
            idx0 = tfams[0]
            base_score = 1.0 + fam_compact[idx0]["pop_prior"] + (0.3 if fam_compact[idx0]["available"] else 0.0)
            add_completion(cname, "product", base_score, tfams)
            for al in parse_json_list(er["aliases_json"])[:4]:
                if norm_str(al) != norm_str(cname):
                    add_completion(al, "alias", base_score * 0.85, tfams)

    # Строим префиксную карту (1..8 символов по каждому слову строки) -> Топ-10 подсказок
    prefix_map: Dict[str, List[int]] = defaultdict(list)
    # Сортируем все подсказки по score по убыванию один раз, чтобы индексы сразу шли в порядке важности
    completions.sort(key=lambda c: c["score"], reverse=True)

    for cid, comp in enumerate(completions):
        words = comp["text_norm"].split()
        prefixes: Set[str] = set()
        # Префиксы всей фразы и каждого отдельного слова (длиной от 1 до 8)
        full = comp["text_norm"]
        for plen in range(1, min(9, len(full) + 1)):
            prefixes.add(full[:plen])
        for w in words[:4]:
            for plen in range(2, min(9, len(w) + 1)):
                prefixes.add(w[:plen])
        for p in prefixes:
            lst = prefix_map[p]
            if len(lst) < 12:
                lst.append(cid)

    trie_payload = {
        "families": fam_compact,
        "completions": completions,
        "prefix_map": dict(prefix_map),
    }
    return sym, trie_payload


def build_popular_by_root(
    families_df: pd.DataFrame,
    categories_df: Optional[pd.DataFrame],
) -> Dict[str, List[Dict[str, Any]]]:
    """Топ-12 популярных доступных family на каждую корневую категорию + '_global' fallback."""
    cat_to_root: Dict[str, str] = {}
    if categories_df is not None and len(categories_df) > 0 and "root_id" in categories_df.columns:
        for _, cr in categories_df.iterrows():
            cid = str(cr.get("category_id") or cr.get("id") or "")
            rid = str(cr.get("root_id") or cid)
            if cid:
                cat_to_root[cid] = rid

    df = families_df.copy()
    df["root_id"] = df["category_id"].astype(str).map(lambda c: cat_to_root.get(c, str(c)))
    df = df.sort_values(
        by=["available_any", "pop_prior", "data_quality_score"],
        ascending=[False, False, False],
    )

    def to_item(r: pd.Series) -> Dict[str, Any]:
        return {
            "family_id": str(r["family_id"]),
            "canonical_name": str(r.get("canonical_name") or ""),
            "vendor": str(r.get("vendor") or ""),
            "product_class": str(r.get("product_class") or "other"),
            "product_type": str(r.get("product_type") or ""),
            "price_min_minor": int(r["price_min_minor"]) if pd.notna(r.get("price_min_minor")) else None,
            "available_any": bool(r.get("available_any")),
            "pop_prior": float(r.get("pop_prior") or 0.0),
        }

    result: Dict[str, List[Dict[str, Any]]] = {
        "_global": [to_item(r) for _, r in df.head(12).iterrows()]
    }
    for root_id, grp in df.groupby("root_id"):
        result[str(root_id)] = [to_item(r) for _, r in grp.head(12).iterrows()]
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка финального индекса data/artifacts/<index_version>/")
    parser.add_argument("--interim-dir", default="data/interim", help="Папка с families.parquet, offers.parquet и т.д.")
    parser.add_argument("--raw-feed", default="data/raw/feed.yml", help="Исходный фид для расчёта feed_sha256")
    parser.add_argument("--embeddings", default="data/interim/embeddings.npy", help="Путь к готовому embeddings.npy")
    parser.add_argument("--dummy-embeddings", action="store_true", help="Сгенерировать временные заглушки (только для smoke-теста без GPU)")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--artifacts-root", default="data/artifacts")
    args = parser.parse_args()

    t0 = time.perf_counter()
    interim = Path(args.interim_dir)
    cfg_dir = Path(args.config_dir)
    artifacts_root = Path(args.artifacts_root)
    raw_feed = Path(args.raw_feed)

    families_path = interim / "families.parquet"
    offers_path = interim / "offers.parquet"
    categories_path = interim / "categories.parquet"
    dqi_path = interim / "data_quality_issues.parquet"
    qc_path = interim / "qc_report.json"

    if not families_path.exists() or not offers_path.exists():
        raise FileNotFoundError(f"Не найдены {families_path} или {offers_path}. Сначала запустите pipelines/ingest!")

    print("1/7 Загружаем таблицы и конфиги...")
    families_df = pd.read_parquet(families_path).reset_index(drop=True)
    offers_df = pd.read_parquet(offers_path)
    categories_df = pd.read_parquet(categories_path) if categories_path.exists() else None
    n_families = len(families_df)
    n_offers = len(offers_df)
    n_categories = len(categories_df) if categories_df is not None else families_df["category_id"].nunique()

    search_cfg = load_yaml_safe(cfg_dir / "search.yaml")
    taxonomy = load_yaml_safe(cfg_dir / "taxonomy.yaml")
    seed_glossary = load_yaml_safe(cfg_dir / "seed_glossary.yaml")
    vendors_seed = load_yaml_safe(cfg_dir / "vendors_seed.yaml")

    feed_sha = sha256_file(raw_feed) if raw_feed.exists() else sha256_file(families_path)
    index_version = make_index_version(feed_sha)
    out_dir = artifacts_root / index_version
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Целевая версия индекса: {index_version} ({n_families} families, {n_offers} offers)")

    # 2. Эмбеддинги
    print("2/7 Проверяем embeddings.npy...")
    emb_src = Path(args.embeddings)
    emb_dst = out_dir / "embeddings.npy"
    emb_model_name = "unknown"
    emb_dim = 1024

    if args.dummy_embeddings and not emb_src.exists():
        rng = np.random.default_rng(42)
        dummy = rng.normal(size=(n_families, 1024)).astype(np.float32)
        dummy /= np.maximum(np.linalg.norm(dummy, axis=1, keepdims=True), 1e-12)
        np.save(emb_dst, dummy)
        emb_model_name = "dummy-smoke-1024"
        emb_dim = 1024
    else:
        if not emb_src.exists():
            raise FileNotFoundError(f"Не найден {emb_src}. Сначала запустите embed_families.py (или укажите --dummy-embeddings).")
        emb_mm = np.load(emb_src, mmap_mode="r")
        if emb_mm.shape[0] != n_families:
            raise ValueError(f"Несовпадение строк: families.parquet={n_families}, embeddings.npy={emb_mm.shape[0]}")
        emb_dim = int(emb_mm.shape[1])
        del emb_mm
        shutil.copy2(emb_src, emb_dst)
        meta_file = emb_src.with_suffix(".meta.json")
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            emb_model_name = str(meta.get("model", "BAAI/bge-m3"))
        else:
            emb_model_name = "BAAI/bge-m3"

    # 3. Сущности и граф
    print("3/7 Строим entities.parquet и graph_edges.parquet...")
    entities_df = build_entities(families_df, offers_df, categories_df, seed_glossary, vendors_seed)
    entities_df.to_parquet(out_dir / "entities.parquet", index=False)

    edges_df = build_graph_edges(families_df, taxonomy, vendors_seed)
    edges_df.to_parquet(out_dir / "graph_edges.parquet", index=False)

    # 4. BM25F и Char-3gram
    print("4/7 Строим разреженные матрицы BM25F (с pymorphy3) и Char-3gram...")
    bm25_mat, bm25_meta = build_bm25f_index(families_df, offers_df, search_cfg)
    sp.save_npz(out_dir / "bm25f.npz", bm25_mat)
    with (out_dir / "bm25f_vocab.pkl").open("wb") as f:
        pickle.dump(bm25_meta, f, protocol=pickle.HIGHEST_PROTOCOL)

    char_mat, char_meta = build_char3gram_index(families_df)
    sp.save_npz(out_dir / "char3gram.npz", char_mat)
    with (out_dir / "char3gram_vocab.pkl").open("wb") as f:
        pickle.dump(char_meta, f, protocol=pickle.HIGHEST_PROTOCOL)

    # 5. SymSpell и Typeahead Trie
    print("5/7 Строим symspell.pkl, typeahead_trie.pkl и popular_by_root.json...")
    sym_spell, trie_payload = build_symspell_and_trie(families_df, entities_df, seed_glossary)
    with (out_dir / "symspell.pkl").open("wb") as f:
        pickle.dump(sym_spell, f, protocol=pickle.HIGHEST_PROTOCOL)
    with (out_dir / "typeahead_trie.pkl").open("wb") as f:
        pickle.dump(trie_payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    popular = build_popular_by_root(families_df, categories_df)
    (out_dir / "popular_by_root.json").write_text(json.dumps(popular, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. Копируем базовые таблицы в иммутабельный снимок
    print("6/7 Сохраняем таблицы снимка и QC-отчёт...")
    families_df.to_parquet(out_dir / "families.parquet", index=False)
    offers_df.to_parquet(out_dir / "offers.parquet", index=False)
    if dqi_path.exists():
        shutil.copy2(dqi_path, out_dir / "data_quality_issues.parquet")
    else:
        pd.DataFrame(columns=["family_id", "field", "issue_type", "details"]).to_parquet(
            out_dir / "data_quality_issues.parquet", index=False
        )

    qc_data = json.loads(qc_path.read_text(encoding="utf-8")) if qc_path.exists() else {}
    qc_data.update(
        {
            "index_version": index_version,
            "n_entities": len(entities_df),
            "n_graph_edges": len(edges_df),
            "bm25f_vocab_size": len(bm25_meta["vocab"]),
            "char3gram_vocab_size": len(char_meta["vocabulary"]),
            "typeahead_completions": len(trie_payload["completions"]),
        }
    )
    (out_dir / "qc_report.json").write_text(json.dumps(qc_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7. Финализируем manifest.json и переключаем current
    print("7/7 Записываем manifest.json и переключаем data/artifacts/current...")
    write_manifest(
        artifact_dir=out_dir,
        index_version=index_version,
        feed_sha256=feed_sha,
        embedding_model=emb_model_name,
        embedding_dim=emb_dim,
        n_offers=n_offers,
        n_families=n_families,
        n_categories=n_categories,
        n_entities=len(entities_df),
        n_edges=len(edges_df),
        extra={"build_sec": round(time.perf_counter() - t0, 2)},
    )
    switch_current_pointer(artifacts_root, index_version)
    print(f"Успешно! Индекс {index_version} собран за {time.perf_counter() - t0:.1f} сек -> {artifacts_root / 'current'}")


if __name__ == "__main__":
    main()