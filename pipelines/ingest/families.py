"""
Сборка family-слоя (families.parquet) из нормализованных офферов (I3).
Содержит все обязательные колонки из CONTRACTS.md B.5.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional

import pandas as pd


def _version_sort_key(ver: Optional[str]) -> Tuple[int, ...]:
    if not ver:
        return (0,)
    nums = re.findall(r"\d+", str(ver))
    return tuple(int(x) for x in nums[:4]) if nums else (0,)


def _pick_representative(grp: pd.DataFrame) -> pd.Series:
    """Выбор представителя (I3): available, price>0, коммерческий сегмент, максимальная версия."""
    def score_row(r: pd.Series) -> tuple:
        avail = 1 if bool(r["available"]) else 0
        priced = 1 if r["price_state"] == "priced" else 0
        seg_ok = 1 if r["segment"] in ("smb", "enterprise", None) else 0
        has_img = 1 if pd.notna(r["image_url"]) and bool(r["image_url"]) else 0
        ver = _version_sort_key(r["version"] if pd.notna(r["version"]) else None)
        price_val = -(int(r["price_minor"])) if pd.notna(r["price_minor"]) else 0
        return (avail, priced, seg_ok, has_img, ver, price_val)

    best_idx = max(grp.index, key=lambda idx: score_row(grp.loc[idx]))
    return grp.loc[best_idx]


def _guess_initial_product_class(rep: pd.Series, cat_path: str) -> str:
    """Быстрая базовая эвристика до запуска этапа 2.2 (classify_rules.py уточнит)."""
    pt = str(rep.get("product_type_raw") or "").lower()
    dt = str(rep.get("delivery_type_raw") or "").lower()
    cp = str(cat_path or "").lower()
    if bool(rep.get("has_hardware_params")) or any(k in pt for k in ("оборуд", "желез", "устройств", "сервер", "техник")) or any(k in cp for k in ("оборудован", "серверы и схд", "оргтехника", "ноутбуки", "сетевое")):
        return "hardware"
    if bool(rep.get("has_hardware_params")) or any(k in cp for k in ("оборудован", "серверы и схд", "оргтехника", "ноутбуки", "сетевое")):
        return "hardware"
    if "облач" in cp or "iaas" in cp or "saas" in cp or "облач" in pt:
        return "cloud_service"
    if "подписк" in pt:
        return "subscription"
    if "сертификат" in pt:
        return "certificate"
    if "обучен" in pt or "вебинар" in pt or "курс" in pt:
        return "training/webinar"
    if "электрон" in dt or "программ" in cp or "по" in pt or pt:
        return "software"
    return "other"


def build_families_table(offers_df: pd.DataFrame, issues_df: pd.DataFrame) -> pd.DataFrame:
    if len(offers_df) == 0:
        return pd.DataFrame()

    # Подсчёт числа SKU на вендора (структурный сигнал веса бренда, п. 2.6)
    vendor_counts: Counter = Counter(offers_df["vendor"].dropna().astype(str).tolist())
    max_vendor_log = math.log1p(max(vendor_counts.values(), default=1))

    # Конфликты/проблемы по family_id для data_quality_score
    issues_per_family: Counter = Counter()
    if len(issues_df) > 0:
        # Игнорируем шумный префикс (он уже очищен) при штрафе качества
        real_issues = issues_df[issues_df["issue_type"] != "noisy_vendor_prefix"]
        issues_per_family.update(real_issues["family_id"].astype(str).tolist())

    family_rows: List[Dict[str, Any]] = []

    for family_id, grp in offers_df.groupby("family_id", sort=False):
        rep = _pick_representative(grp)
        n_variants = len(grp)
        available_any = bool(grp["available"].any())
        avail_ratio = float(grp["available"].mean())

        priced_vals = grp.loc[grp["price_minor"].notna() & (grp["price_minor"] > 0), "price_minor"]
        price_min_minor = int(priced_vals.min()) if len(priced_vals) > 0 else None
        price_max_minor = int(priced_vals.max()) if len(priced_vals) > 0 else None

        disc_vals = grp.loc[grp["discount_pct"].notna(), "discount_pct"]
        discount_best = float(disc_vals.max()) if len(disc_vals) > 0 else None

        # Оси вариантов для карточки товара (S8)
        axes: Dict[str, List[str]] = {}
        for col, axis_name in (
            ("term_months", "term_months"),
            ("qty_users", "qty_users"),
            ("license_type", "license_type"),
            ("segment", "segment"),
        ):
            vals = sorted({str(v) for v in grp[col].dropna().tolist() if str(v).strip()})
            if vals:
                axes[axis_name] = vals

        # Структурный pop_prior (п. 2.6: log вариантов, доля доступных, наличие цены, вес вендора)
        vendor_str = str(rep["vendor"]) if pd.notna(rep["vendor"]) else ""
        v_score = math.log1p(vendor_counts.get(vendor_str, 0)) / max(1e-6, max_vendor_log)
        var_score = min(1.0, math.log1p(n_variants) / math.log1p(50))
        priced_bonus = 0.15 if price_min_minor is not None else 0.0
        pop_prior = round(min(1.0, 0.35 * var_score + 0.30 * avail_ratio + 0.20 * v_score + priced_bonus), 4)

        # Оценка качества карточки (data_quality_score)
        has_img = 1.0 if (pd.notna(rep["image_url"]) and bool(rep["image_url"])) else 0.0
        has_params = 1.0 if (pd.notna(rep["params_text"]) and len(str(rep["params_text"])) > 10) else 0.0
        has_desc = 1.0 if (pd.notna(rep["description_clean"]) and len(str(rep["description_clean"])) > 20) else 0.0
        n_iss = issues_per_family.get(str(family_id), 0)
        dq_score = round(max(0.0, min(1.0, 0.35 * has_img + 0.35 * has_params + 0.30 * has_desc - 0.10 * min(3, n_iss))), 4)

        cat_path = str(rep["category_path"] or "")
        p_class = _guess_initial_product_class(rep, cat_path)
        p_type = str(rep["product_type_raw"] or "") if pd.notna(rep["product_type_raw"]) else ""
        if not p_type and cat_path:
            p_type = cat_path.split("/")[-1].strip()

        family_rows.append(
            {
                "family_id": str(family_id),
                "canonical_name": str(rep["product_name_clean"] or rep["name_raw"]),
                "vendor": vendor_str,
                "product_class": p_class,
                "product_type": p_type,
                "category_id": str(rep["category_id"] or ""),
                "category_path": cat_path,
                "is_ru_registry": bool(grp["is_ru_registry"].any()),
                "available_any": available_any,
                "price_min_minor": price_min_minor,
                "price_max_minor": price_max_minor,
                "discount_best": discount_best,
                "data_quality_score": dq_score,
                "pop_prior": pop_prior,
                "representative_offer_id": str(rep["offer_id"]),
                "n_variants": int(n_variants),
                "variant_axes_json": json.dumps(axes, ensure_ascii=False),
                # Дополнительные поля для 2.2 и 2.3 (build.py)
                "image_url": rep["image_url"] if pd.notna(rep["image_url"]) else None,
                "url": rep["url"] if pd.notna(rep["url"]) else None,
                "params_text": rep["params_text"] if pd.notna(rep["params_text"]) else "",
                "description": rep["description_clean"] if pd.notna(rep["description_clean"]) else "",
                "product_type_raw": rep["product_type_raw"] if pd.notna(rep["product_type_raw"]) else None,
                "delivery_type_raw": rep["delivery_type_raw"] if pd.notna(rep["delivery_type_raw"]) else None,
                "has_hardware_params": bool(grp["has_hardware_params"].any()),
            }
        )

    df = pd.DataFrame(family_rows)
    # Приводим целочисленные nullable-колонки корректно для Parquet
    for col in ("price_min_minor", "price_max_minor"):
        df[col] = pd.array(df[col], dtype="Int64")
    return df