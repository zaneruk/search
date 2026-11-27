#!/usr/bin/env python3
"""
Оркестратор Шага 2.1 (Ingest) и генератор QC-отчёта (I7).

Запуск тестовый (первые 1000 товаров, за 2–3 секунды):
  python -m pipelines.ingest.qc_report --feed data/raw/feed.yml --limit 1000

Полный запуск на всех 250 МБ (займёт ~30–60 секунд):
  python -m pipelines.ingest.qc_report --feed data/raw/feed.yml --gold data/raw/gold.xlsx
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from pipelines.ingest.families import build_families_table
from pipelines.ingest.normalize import normalize_offer
from pipelines.ingest.parse_yml import iter_raw_offers, parse_categories
from pipelines.ingest.quality import build_quality_issues_table


DEFAULT_GOLD_QUERIES = [
    ("astra linux", "/astralinux/astra-linux-special-edition/"),
    ("Microsoft Office для дома и бизнеса", "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024/"),
    ("kaspersky small office security", "/kaspersky/kaspersky-small-office-security/"),
    ("крипто про csp 5.0", "/kripto-pro/cryptopro-csp/"),
    ("anydesk", "/anydesk/anydesk/"),
    ("кибер бэкап", "/cyberprotect/?filter[category][]=1033"),
    ("сканер-вс", "/npo-echelon/eshelon-skaner-vs/"),
    ("мтс", "/webinar/"),
    ("max patrol", "/positive-technologies/maxpatrol-endpoint-protection-platform/"),
    ("365", "/microsoft/microsoft-365-for-family/"),
]


def check_gold_coverage(
    families_df: pd.DataFrame,
    offers_df: pd.DataFrame,
    gold_xlsx: Optional[Path],
) -> List[Dict[str, Any]]:
    pairs = list(DEFAULT_GOLD_QUERIES)
    if gold_xlsx and gold_xlsx.exists():
        try:
            gdf = pd.read_excel(gold_xlsx)
            if len(gdf.columns) >= 2:
                pairs = [
                    (str(r.iloc[0]).strip(), str(r.iloc[1]).strip())
                    for _, r in gdf.iterrows()
                    if pd.notna(r.iloc[0]) and pd.notna(r.iloc[1])
                ]
        except Exception as e:
            print(f"Предупреждение: не удалось прочитать {gold_xlsx}: {e}. Использую список по умолчанию.")

    urls_series = offers_df["url"].dropna().astype(str) if "url" in offers_df.columns else pd.Series([], dtype=str)
    names_series = families_df["canonical_name"].fillna("").astype(str).str.lower()

    results = []
    for query, target_url in pairs:
        # Ищем по пути URL (например /astralinux/astra-linux-special-edition/)
        slug = target_url.split("?")[0].strip("/")
        slug_tail = slug.split("/")[-1] if slug else ""
        url_hit = bool(slug and urls_series.str.contains(slug, case=False, regex=False).any())
        if not url_hit and slug_tail and len(slug_tail) >= 4:
            url_hit = bool(urls_series.str.contains(slug_tail, case=False, regex=False).any())

        # Дополнительная проверка по названию
        tokens = [t.lower() for t in query.split() if len(t) >= 3]
        name_hit = False
        if tokens:
            mask = pd.Series(True, index=names_series.index)
            for t in tokens[:2]:
                mask = mask & names_series.str.contains(t, regex=False)
            name_hit = bool(mask.any())

        results.append(
            {
                "query": query,
                "target_url": target_url,
                "url_found_in_offers": url_hit,
                "name_tokens_found_in_families": name_hit,
            }
        )
    return results


def generate_qc_report(
    offers_df: pd.DataFrame,
    families_df: pd.DataFrame,
    categories: Dict[str, Dict[str, Any]],
    issues_df: pd.DataFrame,
    gold_xlsx: Optional[Path],
    elapsed_sec: float,
) -> Dict[str, Any]:
    n_offers = len(offers_df)
    n_families = len(families_df)

    n_avail_offers = int(offers_df["available"].sum()) if n_offers else 0
    n_priced_offers = int((offers_df["price_state"] == "priced").sum()) if n_offers else 0
    n_valid_promo_offers = int(offers_df["discount_pct"].notna().sum()) if n_offers else 0

    n_avail_fam = int(families_df["available_any"].sum()) if n_families else 0
    n_no_img_fam = int(families_df["image_url"].isna().sum()) if n_families else 0
    n_hw_fam = int((families_df["product_class"] == "hardware").sum()) if n_families else 0
    n_ru_fam = int(families_df["is_ru_registry"].sum()) if n_families else 0

    issue_counts = Counter(issues_df["issue_type"].tolist()) if len(issues_df) else {}
    pclass_counts = Counter(families_df["product_class"].tolist()) if n_families else {}
    top_vendors = Counter(families_df["vendor"].replace("", "UNKNOWN").tolist()).most_common(15)
    top_ptypes = Counter(offers_df["product_type_raw"].dropna().tolist()).most_common(15)

    gold_cov = check_gold_coverage(families_df, offers_df, gold_xlsx)

    return {
        "ingest_sec": round(elapsed_sec, 2),
        "n_categories": len(categories),
        "n_offers": n_offers,
        "offers_available_true": n_avail_offers,
        "offers_available_share": round(n_avail_offers / max(1, n_offers), 4),
        "offers_priced": n_priced_offers,
        "offers_on_request": n_offers - n_priced_offers,
        "offers_valid_promo": n_valid_promo_offers,
        "n_families": n_families,
        "families_available_any": n_avail_fam,
        "families_without_image": n_no_img_fam,
        "families_is_ru_registry": n_ru_fam,
        "families_hardware_heuristic": n_hw_fam,
        "product_class_distribution": dict(pclass_counts),
        "issue_counts": dict(issue_counts),
        "top_15_vendors_by_families": top_vendors,
        "top_15_product_type_raw": top_ptypes,
        "gold10_coverage": gold_cov,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Полный Ingest фида (Шаг 2.1)")
    parser.add_argument("--feed", default="data/raw/feed.yml", help="Путь к Mindbox.yml.gz.xml")
    parser.add_argument("--gold", default="data/raw/gold.xlsx", help="Путь к Excel с 10 gold-запросами")
    parser.add_argument("--out-dir", default="data/interim", help="Папка вывода parquet и qc_report.json")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число офферов для быстрого теста")
    parser.add_argument("--save-params-long", action="store_true", help="Сохранить также params_long.parquet")
    args = parser.parse_args()

    t0 = time.perf_counter()
    feed_path = Path(args.feed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gold_path = Path(args.gold) if args.gold else None

    if not feed_path.exists():
        raise FileNotFoundError(f"Файл фида не найден: {feed_path}")

    print(f"1/5 Читаем дерево категорий из {feed_path} ...")
    categories = parse_categories(feed_path)
    print(f"    Найдено категорий: {len(categories)}")
    cat_df = pd.DataFrame(list(categories.values()))
    cat_df.to_parquet(out_dir / "categories.parquet", index=False)

    print(f"2/5 Потоково парсим и нормализуем <offer> (limit={args.limit}) ...")
    offer_rows: List[Dict[str, Any]] = []
    all_issues: List[Dict[str, str]] = []
    all_params_long: List[tuple] = []

    for i, raw_offer in enumerate(iter_raw_offers(feed_path, limit=args.limit), 1):
        norm_row, off_issues, p_long = normalize_offer(raw_offer, categories)
        offer_rows.append(norm_row)
        for iss in off_issues:
            iss["offer_id"] = norm_row["offer_id"]
            iss["family_id"] = norm_row["family_id"]
            all_issues.append(iss)
        if args.save_params_long:
            all_params_long.extend(p_long)
        if i % 20000 == 0:
            print(f"    Обработано офферов: {i} ...")

    offers_df = pd.DataFrame(offer_rows)
    for col in ("price_minor", "oldprice_minor", "term_months", "qty_users"):
        if col in offers_df.columns:
            offers_df[col] = pd.array(offers_df[col], dtype="Int64")

    print("3/5 Сохраняем offers.parquet и собираем families.parquet ...")
    offers_df.to_parquet(out_dir / "offers.parquet", index=False)

    if args.save_params_long and all_params_long:
        pd.DataFrame(all_params_long, columns=["offer_id", "param_name", "param_value"]).to_parquet(
            out_dir / "params_long.parquet", index=False
        )

    issues_df = build_quality_issues_table(all_issues)
    issues_df.to_parquet(out_dir / "data_quality_issues.parquet", index=False)

    families_df = build_families_table(offers_df, issues_df)
    families_df.to_parquet(out_dir / "families.parquet", index=False)

    print("4/5 Генерируем qc_report.json и проверяем GOLD10 ...")
    elapsed = time.perf_counter() - t0
    qc = generate_qc_report(offers_df, families_df, categories, issues_df, gold_path, elapsed)
    qc_path = out_dir / "qc_report.json"
    qc_path.write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n5/5 ГОТОВО!")
    print(f"    Время: {elapsed:.1f} сек")
    print(f"    Офферов: {qc['n_offers']} (доступных: {qc['offers_available_true']}, с ценой: {qc['offers_priced']}, реальных акций: {qc['offers_valid_promo']})")
    print(f"    Уникальных семейств (families): {qc['n_families']} (доступных: {qc['families_available_any']})")
    print(f"    Классы (эвристика): {qc['product_class_distribution']}")
    print(f"    Файлы сохранены в: {out_dir}")


if __name__ == "__main__":
    main()