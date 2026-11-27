"""S8: Variant Resolution (family -> лучший OfferView + оси вариантов).

Строго соблюдает инвариант I2:
  price_minor is None <=> price_state == "on_request" (цена 0 никогда не отдаётся).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from services.api.app.schemas import ClientContext, OfferView, PriceState
from services.api.core.index_state import IndexState
from services.api.growth.promo import compute_discount_pct
from services.api.nlp import NlpAnalysis
from services.api.nlp.config import basic_norm


@dataclass(frozen=True)
class VariantResolution:
    representative: OfferView
    n_variants: int
    variant_axes: Dict[str, List[str]]
    price_min_minor: Optional[int]


def _opt_int(val: Any) -> Optional[int]:
    if val is None or pd.isna(val):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _opt_str(val: Any) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return s


def _to_bool(val: Any) -> bool:
    if val is None or pd.isna(val):
        return False
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "да")
    return bool(val)


def _offer_to_view(row: Dict[str, Any]) -> OfferView:
    avail = _to_bool(row.get("available"))
    raw_price = _opt_int(row.get("price_minor"))
    if raw_price is None or raw_price <= 0:
        price_minor: Optional[int] = None
        price_state = PriceState.on_request
    else:
        price_minor = raw_price
        price_state = PriceState.priced

    raw_old = _opt_int(row.get("oldprice_minor"))
    oldprice_minor = raw_old if (raw_old is not None and raw_old > 0) else None
    discount_pct = compute_discount_pct(price_minor, oldprice_minor)

    return OfferView(
        offer_id=str(row.get("offer_id") or ""),
        name_raw=str(row.get("name_raw") or row.get("product_name_clean") or ""),
        available=avail,
        price_state=price_state,
        price_minor=price_minor,
        oldprice_minor=oldprice_minor,
        discount_pct=discount_pct,
        term_months=_opt_int(row.get("term_months")),
        qty_users=_opt_int(row.get("qty_users")),
        license_type=_opt_str(row.get("license_type")),
        segment_fit=_opt_str(row.get("segment")),
        url=_opt_str(row.get("url")),
    )


def resolve_variants(
    family_id: str,
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
    *,
    preferred_offer_id: Optional[str] = None,
) -> Optional[VariantResolution]:
    rows = index_state.offer_rows(family_id)
    if not rows:
        return None

    attrs = analysis.qu.attrs
    target_seg = attrs.segment or ctx.segment.value

    views: List[Tuple[Tuple[int, int, int, int, int, str], OfferView]] = []
    priced_values: List[int] = []
    axes_sets: Dict[str, List[str]] = {
        "term_months": [],
        "qty_users": [],
        "license_type": [],
        "segment": [],
        "version": [],
    }

    def add_axis(axis: str, val: Optional[str]) -> None:
        if val and val not in axes_sets[axis] and len(axes_sets[axis]) < 8:
            axes_sets[axis].append(val)

    for r in rows:
        view = _offer_to_view(r)
        if view.price_minor is not None and view.price_minor > 0:
            priced_values.append(view.price_minor)

        if view.term_months:
            add_axis("term_months", f"{view.term_months} мес.")
        if view.qty_users:
            add_axis("qty_users", f"{view.qty_users} шт.")
        add_axis("license_type", view.license_type)
        add_axis("segment", view.segment_fit)
        add_axis("version", _opt_str(r.get("version")))

        seg_norm = basic_norm(view.segment_fit or "")
        seg_score = 2 if seg_norm == target_seg else (1 if not seg_norm or seg_norm in ("smb", "commercial") else 0)
        if target_seg in ("smb", "enterprise", "gov") and seg_norm in ("edu", "academic", "personal"):
            seg_score = -1

        attr_score = 0
        if attrs.term_months and view.term_months == attrs.term_months:
            attr_score += 2
        if attrs.qty_users and view.qty_users == attrs.qty_users:
            attr_score += 2
        ver_str = _opt_str(r.get("version")) or ""
        if attrs.version and attrs.version.lower() in (ver_str + " " + view.name_raw).lower():
            attr_score += 2
        if attrs.license_type and view.license_type and attrs.license_type in basic_norm(view.license_type):
            attr_score += 1

        rep_bonus = 1 if (preferred_offer_id and view.offer_id == preferred_offer_id) else 0
        # Сортируем: доступный > с ценой > совпадение атрибутов > сегмент > preferred > детерминированно по offer_id
        rank_key = (
            1 if view.available else 0,
            1 if view.price_state == PriceState.priced else 0,
            attr_score,
            seg_score,
            rep_bonus,
            view.offer_id,
        )
        views.append((rank_key, view))

    views.sort(key=lambda x: (-x[0][0], -x[0][1], -x[0][2], -x[0][3], -x[0][4], x[0][5]))
    best_view = views[0][1]
    clean_axes = {k: v for k, v in axes_sets.items() if v}

    return VariantResolution(
        representative=best_view,
        n_variants=len(rows),
        variant_axes=clean_axes,
        price_min_minor=min(priced_values) if priced_values else None,
    )