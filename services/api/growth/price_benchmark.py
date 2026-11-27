"""Внутрикаталожное сравнение цен (CONTRACTS.md A.6).

Сравнивает ТОЛЬКО внутри каталога по ключу (product_type, segment, license_type),
нормализуя цену к `price_per_user_year_minor` при `n_compared >= 3`.
"""
from __future__ import annotations

import statistics
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from services.api.app.schemas import Badge, OfferView, PriceState
from services.api.core.index_state import IndexState
from services.api.nlp.config import basic_norm


@dataclass(frozen=True)
class _PeerUnit:
    family_id: str
    unit_minor: int


@dataclass(frozen=True)
class _BenchmarkCache:
    index_version: str
    groups: Dict[Tuple[str, str, str], List[_PeerUnit]]


_cache_lock = threading.Lock()
_cache: Optional[_BenchmarkCache] = None


def price_per_user_year_minor(offer: OfferView) -> Optional[int]:
    if (
        offer.price_state != PriceState.priced
        or offer.price_minor is None
        or offer.price_minor <= 0
        or not offer.term_months
        or offer.term_months <= 0
        or not offer.qty_users
        or offer.qty_users <= 0
    ):
        return None
    val = offer.price_minor * (12.0 / float(offer.term_months)) / float(offer.qty_users)
    return max(1, int(round(val)))


def _safe_pos_int(val) -> Optional[int]:
    if val is None or pd.isna(val):
        return None
    try:
        v = int(float(val))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _get_cache(index_state: IndexState) -> _BenchmarkCache:
    global _cache
    with _cache_lock:
        if _cache is not None and _cache.index_version == index_state.index_version:
            return _cache

    groups: Dict[Tuple[str, str, str], List[_PeerUnit]] = defaultdict(list)
    if index_state.is_loaded and index_state.family_dicts:
        for fam in index_state.family_dicts:
            fid = str(fam.get("family_id") or "")
            pt = str(fam.get("product_type") or "").strip()
            rep_oid = str(fam.get("representative_offer_id") or "").strip()
            if not fid or not pt:
                continue
            offers = index_state.offer_rows(fid)
            if not offers:
                continue
            rep = next((o for o in offers if str(o.get("offer_id") or "") == rep_oid), offers[0])
            price = _safe_pos_int(rep.get("price_minor"))
            term = _safe_pos_int(rep.get("term_months"))
            qty = _safe_pos_int(rep.get("qty_users"))
            if price is None or term is None or qty is None:
                continue
            seg_raw = rep.get("segment")
            lic_raw = rep.get("license_type")
            seg = basic_norm(str(seg_raw)) if seg_raw is not None and not pd.isna(seg_raw) else "smb"
            lic = basic_norm(str(lic_raw)) if lic_raw is not None and not pd.isna(lic_raw) else ""
            if not lic or lic in ("nan", "none", "<na>"):
                continue
            unit = max(1, int(round(price * (12.0 / term) / qty)))
            groups[(pt, seg or "smb", lic)].append(_PeerUnit(family_id=fid, unit_minor=unit))

    built = _BenchmarkCache(index_version=index_state.index_version, groups=dict(groups))
    with _cache_lock:
        _cache = built
    return built


def evaluate_price_advantage(
    family_id: str,
    product_type: str,
    representative: OfferView,
    index_state: IndexState,
    *,
    min_compared: int = 3,
    min_advantage_pct: float = 10.0,
) -> Optional[Badge]:
    my_unit = price_per_user_year_minor(representative)
    if my_unit is None or not product_type or not representative.license_type:
        return None

    seg = basic_norm(representative.segment_fit or "smb") or "smb"
    lic = basic_norm(representative.license_type)
    cache = _get_cache(index_state)
    peers = cache.groups.get((product_type, seg, lic), [])
    other_peers = [p for p in peers if p.family_id != family_id]
    n_compared = len(peers) if any(p.family_id == family_id for p in peers) else len(other_peers) + 1
    if len(other_peers) < max(2, min_compared - 1) or n_compared < min_compared:
        return None

    median_unit = statistics.median([p.unit_minor for p in other_peers])
    if median_unit <= my_unit:
        return None

    adv_pct = round((median_unit - my_unit) / median_unit * 100.0, 1)
    if adv_pct < min_advantage_pct or adv_pct > 85.0:
        return None

    # Ближайший более дорогой аналог около медианы для прозрачности basis
    compared_peer = min(
        (p for p in other_peers if p.unit_minor >= median_unit),
        key=lambda p: p.unit_minor,
        default=max(other_peers, key=lambda p: p.unit_minor),
    )
    return Badge(
        type="price_advantage",
        label=f"Выгоднее аналога на {adv_pct:g}% за польз./год",
        basis={
            "n_compared": n_compared,
            "unit": "price_per_user_year_minor",
            "compared_family_id": compared_peer.family_id,
            "my_unit_minor": my_unit,
            "median_unit_minor": int(round(median_unit)),
            "advantage_pct": adv_pct,
        },
    )