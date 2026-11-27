# 400 детерминированных запросов из фида (13 классов, dev/holdout)
"""Детерминированный генератор SYN-A (CONTRACTS.md A.2, A.3): ~400 запросов из фида без LLM.

Сплит по family_id (A.3):
  split = 'dev' if int(sha1(family_id)[:8], 16) % 10 < 7 else 'holdout'

Квоты (A.2):
  exact_name 60, brand 40, brand_attr 50, generic_type 40, relational 30,
  typo_1edit 40, typo_2edit 25, translit 30, layout 20, split_join 25,
  numeric_only 10, out_of_catalog 20, nonsense 10  (итого 400).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from services.api.core.index_state import get_index_state
from services.api.nlp.config import basic_norm
from services.api.nlp.layout import convert_ru_to_en
from services.api.nlp.translit import ru_to_lat

_EN_TO_RU_SIMPLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "и", "z": "з",
}
_CYR_KEYS = set("йцукенгшщзхъфывапролджэячсмитьбю")
_LAT_KEYS = set("abcdefghijklmnopqrstuvwxyz")


def family_split(family_id: str) -> str:
    h = int(hashlib.sha1(family_id.encode("utf-8")).hexdigest()[:8], 16)
    return "dev" if (h % 10) < 7 else "holdout"


def _simple_lat_to_cyr(text: str) -> str:
    s = (text or "").lower()
    for dg, ru in (("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("th", "т"), ("ph", "ф"), ("oo", "у"), ("ee", "и")):
        s = s.replace(dg, ru)
    return "".join(_EN_TO_RU_SIMPLE.get(ch, ch) for ch in s)


def _corrupt_1edit(word: str, rng: random.Random) -> Optional[str]:
    if len(word) < 4:
        return None
    pos = rng.randint(1, len(word) - 2)
    op = rng.choice(("del", "swap", "sub"))
    if op == "del":
        return word[:pos] + word[pos + 1 :]
    if op == "swap" and word[pos] != word[pos + 1]:
        return word[:pos] + word[pos + 1] + word[pos] + word[pos + 2 :]
    alphabet = "аеиопрсткнвлм" if "а" <= word[pos] <= "я" else "aeiorstknlmp"
    repl = rng.choice([c for c in alphabet if c != word[pos]])
    return word[:pos] + repl + word[pos + 1 :]


def _corrupt_2edit(word: str, rng: random.Random) -> Optional[str]:
    if len(word) < 8:
        return None
    w1 = _corrupt_1edit(word, rng)
    if not w1:
        return None
    w2 = _corrupt_1edit(w1, rng)
    return w2 if w2 and w2 != word else None


def _short_product_name(canonical: str) -> str:
    s = re.sub(r"\([^)]*\)", "", canonical or "").strip()
    s = re.split(r"[,:;]", s)[0].strip()
    words = s.split()
    return " ".join(words[:4]) if len(words) > 4 else s


def generate_syn_a(out_path: Path = Path("eval/datasets/syn_a/queries.jsonl"), seed: int = 42) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    idx = get_index_state()
    if not idx.is_loaded or idx.families is None:
        raise RuntimeError("IndexState is not loaded; cannot build SYN-A")

    f_df = idx.families.copy()
    o_df = idx.offers
    f_df["split"] = f_df["family_id"].astype(str).map(family_split)

    # Приоритет читаемым популярным позициям
    f_df = f_df.sort_values(by=["available_any", "pop_prior"], ascending=[False, False]).reset_index(drop=True)

    # Индекс соседей (grade 2 = тот же vendor и product_type)
    g2_pool: Dict[Tuple[str, str], List[str]] = {}
    g1_pool: Dict[str, List[str]] = {}
    for _, r in f_df.iterrows():
        fid = str(r["family_id"])
        ven = basic_norm(str(r.get("vendor") or ""))
        pt = str(r.get("product_type") or "")
        if pt:
            g1_pool.setdefault(pt, []).append(fid)
            if ven:
                g2_pool.setdefault((ven, pt), []).append(fid)

    # Справочник атрибутов по family из offers.parquet
    offer_attrs: Dict[str, Dict[str, Any]] = {}
    if o_df is not None and len(o_df) > 0:
        for _, orow in o_df.dropna(subset=["family_id"]).head(40000).iterrows():
            fid = str(orow["family_id"])
            if fid in offer_attrs:
                continue
            term = orow.get("term_months")
            qty = orow.get("qty_users")
            ver = orow.get("version")
            if pd.notna(term) or pd.notna(qty) or (pd.notna(ver) and str(ver).strip()):
                offer_attrs[fid] = {
                    "term": int(float(term)) if pd.notna(term) else None,
                    "qty": int(float(qty)) if pd.notna(qty) else None,
                    "ver": str(ver).strip() if pd.notna(ver) and str(ver).strip() else None,
                }

    rows = f_df.to_dict("records")
    rng.shuffle(rows)

    records: List[Dict[str, Any]] = []
    seen_queries: set = set()

    def make_rec(query: str, q_class: str, r: Dict[str, Any], *, is_ooc: bool = False, is_nonsense: bool = False) -> Optional[Dict[str, Any]]:
        qn = basic_norm(query)
        if not qn or len(qn) < 2 or qn in seen_queries:
            return None
        seen_queries.add(qn)
        fid = str(r["family_id"]) if r else ""
        ven = basic_norm(str(r.get("vendor") or "")) if r else ""
        pt = str(r.get("product_type") or "") if r else ""
        g2 = [x for x in g2_pool.get((ven, pt), []) if x != fid][:25]
        return {
            "id": f"syna_{len(records) + 1:04d}",
            "query": query.strip(),
            "query_class": q_class,
            "split": family_split(fid) if fid else ("dev" if len(records) % 10 < 7 else "holdout"),
            "target_family_id": fid or None,
            "grade2_family_ids": g2,
            "target_product_type": pt or None,
            "target_vendor": r.get("vendor") if r else None,
            "expected_ooc": is_ooc,
            "is_nonsense": is_nonsense,
            "data_tier": "derived",
        }

    # 1. exact_name (60)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "exact_name") >= 60:
            break
        q = _short_product_name(str(r.get("canonical_name") or ""))
        if len(q.split()) >= 2:
            rec = make_rec(q, "exact_name", r)
            if rec:
                records.append(rec)

    # 2. brand (40)
    seen_brands: set = set()
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "brand") >= 40:
            break
        ven = str(r.get("vendor") or "").strip()
        v_norm = basic_norm(ven)
        if len(v_norm) >= 4 and v_norm not in seen_brands:
            seen_brands.add(v_norm)
            rec = make_rec(ven, "brand", r)
            if rec:
                records.append(rec)

    # 3. brand_attr (50)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "brand_attr") >= 50:
            break
        fid = str(r["family_id"])
        attrs = offer_attrs.get(fid)
        base_q = _short_product_name(str(r.get("canonical_name") or ""))
        if not attrs or not base_q:
            continue
        suffix = ""
        if attrs.get("term"):
            suffix = f"{attrs['term']} мес"
        elif attrs.get("qty"):
            suffix = f"{attrs['qty']} пользователей"
        elif attrs.get("ver"):
            suffix = attrs["ver"]
        if suffix and suffix not in base_q.lower():
            rec = make_rec(f"{base_q} {suffix}", "brand_attr", r)
            if rec:
                records.append(rec)

    # 4. generic_type (40)
    seen_types: set = set()
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "generic_type") >= 40:
            break
        pt = str(r.get("product_type") or "").strip()
        if pt and pt not in seen_types and len(pt.split()) <= 4:
            seen_types.add(pt)
            rec = make_rec(pt.lower(), "generic_type", r)
            if rec:
                records.append(rec)

    # 5. relational (30)
    rel_templates = ["антивирус для сервера", "резервное копирование для бизнеса", "по для защиты от утечек", "офис для малого бизнеса", "решение для виртуализации"]
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "relational") >= 30:
            break
        cname = str(r.get("canonical_name") or "").lower()
        if " для " in cname:
            q = _short_product_name(str(r.get("canonical_name") or ""))
            rec = make_rec(q, "relational", r)
            if rec:
                records.append(rec)
    for tpl in rel_templates:
        if sum(1 for x in records if x["query_class"] == "relational") >= 30:
            break
        rec = make_rec(tpl, "relational", rows[0])
        if rec:
            records.append(rec)

    # 6. typo_1edit (40)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "typo_1edit") >= 40:
            break
        base_q = _short_product_name(str(r.get("canonical_name") or ""))
        words = base_q.split()
        cand_idx = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha()]
        if not cand_idx:
            continue
        i = rng.choice(cand_idx)
        corrupted = _corrupt_1edit(words[i].lower(), rng)
        if corrupted:
            words[i] = corrupted
            rec = make_rec(" ".join(words), "typo_1edit", r)
            if rec:
                records.append(rec)

    # 7. typo_2edit (25)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "typo_2edit") >= 25:
            break
        base_q = _short_product_name(str(r.get("canonical_name") or ""))
        words = base_q.split()
        cand_idx = [i for i, w in enumerate(words) if len(w) >= 8 and w.isalpha()]
        if not cand_idx:
            continue
        i = cand_idx[0]
        corrupted = _corrupt_2edit(words[i].lower(), rng)
        if corrupted:
            words[i] = corrupted
            rec = make_rec(" ".join(words), "typo_2edit", r)
            if rec:
                records.append(rec)

    # 8. translit (30)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "translit") >= 30:
            break
        ven = str(r.get("vendor") or "").strip().split()[0]
        low = ven.lower()
        if len(low) < 5:
            continue
        if all(c in _LAT_KEYS for c in low):
            tr = _simple_lat_to_cyr(low)
        elif all(c in _CYR_KEYS for c in low):
            tr = ru_to_lat(low)
        else:
            continue
        if tr and tr != low:
            rec = make_rec(tr, "translit", r)
            if rec:
                records.append(rec)

    # 9. layout (20)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "layout") >= 20:
            break
        base_q = _short_product_name(str(r.get("canonical_name") or ""))
        words = [w.lower() for w in base_q.split() if len(w) >= 5 and all(c in _CYR_KEYS for c in w.lower())]
        if not words:
            continue
        converted = convert_ru_to_en(words[0])
        if converted and any(ch.isalpha() for ch in converted):
            rec = make_rec(converted, "layout", r)
            if rec:
                records.append(rec)

    # 10. split_join (25)
    for r in rows:
        if sum(1 for x in records if x["query_class"] == "split_join") >= 25:
            break
        base_q = _short_product_name(str(r.get("canonical_name") or ""))
        words = base_q.split()
        if len(words) >= 2 and words[0].isalpha() and words[1].isalpha() and len(words[0]) >= 3 and len(words[1]) >= 3:
            joined = f"{words[0]}{words[1]}" + (" " + " ".join(words[2:]) if len(words) > 2 else "")
            rec = make_rec(joined, "split_join", r)
            if rec:
                records.append(rec)

    # 11. numeric_only (10)
    num_candidates: List[Tuple[str, Dict[str, Any]]] = []
    for r in rows:
        cname = str(r.get("canonical_name") or "")
        m = re.search(r"\b(\d{3,4})\b", cname)
        if m:
            num_candidates.append((m.group(1), r))
    for num_str, r in num_candidates:
        if sum(1 for x in records if x["query_class"] == "numeric_only") >= 10:
            break
        rec = make_rec(num_str, "numeric_only", r)
        if rec:
            records.append(rec)

    # 12. out_of_catalog (20)
    ooc_pool = [
        "MDaemon Email Server", "Kerio Connect", "Zimbra Collaboration", "TeamViewer Enterprise",
        "Atlassian Jira Software", "Atlassian Confluence", "JetBrains IntelliJ IDEA",
        "GitLab Ultimate", "Docker Business", "Terraform Enterprise", "HashiCorp Vault",
        "CrowdStrike Falcon", "SentinelOne Singularity", "Palo Alto Cortex XDR",
        "Veeam Backup Replication", "VMware vSphere Enterprise", "Snowflake Enterprise",
        "Datadog APM", "Figma Organization", "Notion Enterprise",
    ]
    for q_ooc in ooc_pool[:20]:
        rec = make_rec(q_ooc, "out_of_catalog", {}, is_ooc=True)
        if rec:
            records.append(rec)

    # 13. nonsense (10)
    nonsense_pool = ["абракадабра123", "фывфывфыв", "zzzzqqqqxxxx", "йцукйцук", "999999999", "хзхзхзхз", "qwertyuiopasdf", "тесттесттест", "ммммпппп", "!@#$%"]
    for q_ns in nonsense_pool[:10]:
        rec = make_rec(q_ns, "nonsense", {}, is_nonsense=True)
        if rec:
            records.append(rec)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Генератор детерминированного набора SYN-A")
    parser.add_argument("--out", default="eval/datasets/syn_a/queries.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    recs = generate_syn_a(Path(args.out), seed=args.seed)
    by_cls: Dict[str, int] = {}
    for r in recs:
        by_cls[r["query_class"]] = by_cls.get(r["query_class"], 0) + 1
    print(f"SYN-A generated: {len(recs)} queries -> {args.out}")
    print("Classes:", by_cls)


if __name__ == "__main__":
    main()