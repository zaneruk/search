#!/usr/bin/env python3
"""
Маппинг GOLD10 (data/raw/gold.xlsx) в eval/datasets/gold_map.csv строго по A.2 CONTRACTS.md:
  query,target_url,target_kind,target_family_id,target_entity_id,present_in_feed,notes

Запуск:
  python -m pipelines.gold.map_gold --families data/interim/families.parquet --gold data/raw/gold.xlsx
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

DEFAULT_GOLD = [
    ("astra linux", "https://store.softline.ru/astralinux/astra-linux-special-edition/"),
    ("Microsoft Office для дома и бизнеса", "https://store.softline.ru/microsoft/microsoft-office-dlya-doma-i-biznesa-2024/"),
    ("kaspersky small office security", "https://store.softline.ru/kaspersky/kaspersky-small-office-security/"),
    ("крипто про csp 5.0", "https://store.softline.ru/kripto-pro/cryptopro-csp/"),
    ("anydesk", "https://store.softline.ru/anydesk/anydesk/"),
    ("кибер бэкап", "https://store.softline.ru/cyberprotect/?filter%5Bcategory%5D%5B%5D=1033"),
    ("сканер-вс", "https://store.softline.ru/npo-echelon/eshelon-skaner-vs/"),
    ("мтс", "https://store.softline.ru/webinar/"),
    ("max patrol", "https://store.softline.ru/positive-technologies/maxpatrol-endpoint-protection-platform/"),
    ("365", "https://store.softline.ru/microsoft/microsoft-365-for-family/"),
]

RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def norm_str(s: str) -> str:
    return " ".join(str(s or "").lower().replace("ё", "е").split())


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


def detect_target_kind(url: str) -> str:
    low = url.lower()
    if "/webinar" in low or "/events" in low:
        return "content"
    if "filter" in low or "/category" in low:
        return "category"
    return "product"


def extract_url_hints(url: str) -> Tuple[str, List[str]]:
    path = url.split("?")[0]
    path = re.sub(r"^https?://[^/]+", "", path).strip("/")
    parts = [p for p in path.split("/") if p]
    vendor_hint = parts[0].replace("-", " ") if parts else ""
    slug_tokens = []
    if len(parts) >= 2:
        slug_tokens = [t for t in parts[-1].split("-") if len(t) >= 2]
    elif parts:
        slug_tokens = [t for t in parts[0].split("-") if len(t) >= 2]
    return vendor_hint, slug_tokens


def find_best_family(
    query: str,
    target_url: str,
    families_df: pd.DataFrame,
) -> Tuple[Optional[str], str, str]:
    """Возвращает (family_id, matched_name, notes)."""
    q_low = norm_str(query)
    q_tokens = [t for t in re.findall(r"[a-zа-яё0-9]+", q_low) if len(t) >= 2]
    vendor_hint, slug_tokens = extract_url_hints(target_url)

    search_tokens = set(q_tokens + slug_tokens)
    url_low = target_url.lower()

    # Подсказки исключительно по целевому URL из gold.xlsx для точного оффлайн-маппинга ground-truth
    extra_vendor_aliases: List[str] = []
    if "astra-linux-special-edition" in url_low or "astralinux" in url_low:
        search_tokens.update(["astra", "linux", "alse", "орел", "воронеж", "смоленск", "рабочей", "станции"])
        extra_vendor_aliases.append("группа астра")
    if "doma-i-biznesa" in url_low:
        search_tokens.update(["дома", "бизнеса", "home", "business", "2024"])
    if "365" in q_tokens:
        search_tokens.update(["microsoft", "365", "family", "семьи"])
    if "skaner-vs" in url_low:
        search_tokens.update(["сканер", "эшелон"])
    if "cyberprotect" in url_low:
        search_tokens.update(["кибер", "бэкап", "backup", "киберпротект"])
    if "cryptopro" in url_low:
        search_tokens.update(["криптопро", "cryptopro", "csp"])
    if "maxpatrol" in url_low:
        search_tokens.update(["maxpatrol", "endpoint"])

    wants_cert = any(w in q_low for w in ("сертификат", "поддерж", "сопровожден"))
    best_score = -1.0
    best_row = None

    for _, row in families_df.iterrows():
        cname = norm_str(row.get("canonical_name"))
        vendor = norm_str(row.get("vendor"))
        cpath = norm_str(row.get("category_path"))
        pclass = norm_str(row.get("product_class"))
        url_row = norm_str(row.get("url"))

        score = 0.0
        for st in slug_tokens:
            if len(st) >= 4 and st in url_row:
                score += 2.0

        vendor_matched = False
        if vendor_hint and (vendor_hint in vendor or any(vh in vendor for vh in vendor_hint.split() if len(vh) >= 3)):
            vendor_matched = True
        elif extra_vendor_aliases and any(va in vendor for va in extra_vendor_aliases):
            vendor_matched = True
        if vendor_matched:
            score += 3.5

        for tok in search_tokens:
            if tok in cname:
                score += 2.5 if len(tok) >= 4 else 1.5
            elif tok in cpath:
                score += 1.5

        if q_low in cname:
            score += 6.0

        # Сильный штраф для сертификатов техподдержки, если запрос про сам продукт
        if not wants_cert and (pclass == "certificate" or "сертификат" in cname or "техподдерж" in cname or "сопровожден" in cname):
            score -= 12.0

        # Доп. штраф, если это плагин/техподдержка для другого продукта (например СУБД Tantor)
        if "astra" in q_low and ("субд" in cname or "tantor" in cname or "ald" in cname or "dcimanager" in cname):
            score -= 8.0

        if score <= 2.0:
            continue

        score += 0.5 * float(row.get("pop_prior") or 0.0)
        if bool(row.get("available_any")):
            score += 0.3

        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None:
        return None, "", "Не найдено в families.parquet"
    return str(best_row["family_id"]), str(best_row["canonical_name"]), f"score={best_score:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Генерация eval/datasets/gold_map.csv (Шаг 2.2)")
    parser.add_argument("--families", default="data/interim/families.parquet")
    parser.add_argument("--categories", default="data/interim/categories.parquet")
    parser.add_argument("--gold", default="data/raw/gold.xlsx")
    parser.add_argument("--out", default="eval/datasets/gold_map.csv")
    args = parser.parse_args()

    fam_df = pd.read_parquet(args.families)
    cat_df = pd.read_parquet(args.categories) if Path(args.categories).exists() else pd.DataFrame()

    pairs = list(DEFAULT_GOLD)
    gold_path = Path(args.gold)
    if gold_path.exists():
        gdf = pd.read_excel(gold_path)
        if len(gdf.columns) >= 2:
            pairs = [
                (str(r.iloc[0]).strip(), str(r.iloc[1]).strip())
                for _, r in gdf.iterrows()
                if pd.notna(r.iloc[0]) and pd.notna(r.iloc[1])
            ]

    rows: List[Dict[str, str]] = []
    for query, target_url in pairs:
        kind = detect_target_kind(target_url)
        fid, matched_name, note = find_best_family(query, target_url, fam_df)
        entity_id = ""
        present = fid is not None

        if kind == "content":
            entity_id = f"e_content_{slugify('Вебинары и онлайн-мероприятия Softline (МТС Линк и др.)_' + target_url)}"
            present = True
            note = f"CONTENT page ({target_url}); product_fallback={matched_name[:50]}"
        elif kind == "category":
            m_cat = re.search(r"filter(?:%5B|\[)category(?:%5D|\])(?:%5B|\[)(?:%5D|\])=(\d+)", target_url)
            if m_cat and len(cat_df) > 0:
                cid = f"c_{m_cat.group(1)}"
                sub_cat = cat_df[cat_df["category_id"].astype(str) == cid]
                if len(sub_cat) > 0:
                    cname = str(sub_cat.iloc[0]["name"])
                    entity_id = f"e_category_{slugify(f'{cid}_{cname}')}"
            if not entity_id and fid:
                entity_id = f"e_family_{slugify(fid)}"
            note = f"CATEGORY filter; matched_family={matched_name[:50]}"
        else:
            if fid:
                entity_id = f"e_family_{slugify(fid)}"
                note = f"matched={matched_name[:60]} ({note})"

        rows.append(
            {
                "query": query,
                "target_url": target_url,
                "target_kind": kind,
                "target_family_id": fid or "",
                "target_entity_id": entity_id,
                "present_in_feed": "true" if present else "false",
                "notes": note,
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df = pd.DataFrame(rows)
    out_df.to_csv(out_path, index=False, encoding="utf-8")

    print(f"Сохранено {out_path} ({len(out_df)} строк):\n")
    print(out_df[["query", "target_kind", "target_family_id", "present_in_feed", "notes"]].to_string(index=False))


if __name__ == "__main__":
    main()