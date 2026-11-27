"""Атрибутные сигналы по family/offers (S5/S6/S8).

Жёсткий фильтр по `available=true` НЕ применяется (32.8% каталога), поэтому здесь
собираются мягкие признаки совпадения по сегменту, сроку, количеству, версии, лицензии и реестру РФ.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from services.api.app.schemas import ClientContext
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.nlp.config import basic_norm


@dataclass(frozen=True)
class FamilySignals:
    available_any: bool
    is_ru_registry: bool
    has_priced_offer: bool
    has_academic_only: bool
    segments: Tuple[str, ...]
    terms: Tuple[int, ...]
    qtys: Tuple[int, ...]
    license_types: Tuple[str, ...]
    versions: Tuple[str, ...]


@dataclass(frozen=True)
class CandidateAttributes:
    available_any: bool
    has_priced_offer: bool
    is_ru_registry: bool
    academic_only: bool
    segment_fit: float          # 1.0 match | 0.5 neutral | 0.0 mismatch
    attr_bonus: float           # бонус за совпадение term/qty/version/license из QU
    matched_attrs: Tuple[str, ...]


_cache_lock = threading.Lock()
_cache_ver: Optional[str] = None
_cache_map: Dict[int, FamilySignals] = {}


def _to_int(val: Any) -> Optional[int]:
    if val is None or pd.isna(val):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _to_str(val: Any) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return basic_norm(s)


def _build_signals_map(index_state: IndexState) -> Dict[int, FamilySignals]:
    global _cache_ver, _cache_map
    with _cache_lock:
        if _cache_ver == index_state.index_version and _cache_map:
            return _cache_map

    f_df = index_state.families
    o_df = index_state.offers
    n_fam = len(f_df)

    avail_col = f_df["available_any"].fillna(False).astype(bool).tolist() if "available_any" in f_df.columns else [False] * n_fam
    ru_col = f_df["is_ru_registry"].fillna(False).astype(bool).tolist() if "is_ru_registry" in f_df.columns else [False] * n_fam
    pmin_col = f_df["price_min_minor"].tolist() if "price_min_minor" in f_df.columns else [None] * n_fam

    seg_by_fid: Dict[str, set] = {}
    term_by_fid: Dict[str, set] = {}
    qty_by_fid: Dict[str, set] = {}
    lic_by_fid: Dict[str, set] = {}
    ver_by_fid: Dict[str, set] = {}

    if o_df is not None and len(o_df) > 0:
        cols = [c for c in ("family_id", "segment", "term_months", "qty_users", "license_type", "version") if c in o_df.columns]
        cpos = {c: i for i, c in enumerate(cols)}
        i_seg = cpos.get("segment")
        i_term = cpos.get("term_months")
        i_qty = cpos.get("qty_users")
        i_lic = cpos.get("license_type")
        i_ver = cpos.get("version")

        for row in o_df[cols].itertuples(index=False, name=None):
            fid = str(row[0])
            if i_seg is not None:
                s = _to_str(row[i_seg])
                if s:
                    seg_by_fid.setdefault(fid, set()).add(s)
            if i_term is not None:
                t = _to_int(row[i_term])
                if t is not None:
                    term_by_fid.setdefault(fid, set()).add(t)
            if i_qty is not None:
                q = _to_int(row[i_qty])
                if q is not None:
                    qty_by_fid.setdefault(fid, set()).add(q)
            if i_lic is not None:
                lt = _to_str(row[i_lic])
                if lt:
                    lic_by_fid.setdefault(fid, set()).add(lt)
            if i_ver is not None:
                v = _to_str(row[i_ver])
                if v:
                    ver_by_fid.setdefault(fid, set()).add(v)

    fids = f_df["family_id"].astype(str).tolist()
    cnames = f_df["canonical_name"].fillna("").astype(str).tolist()
    out: Dict[int, FamilySignals] = {}
    for pos, fid in enumerate(fids):
        segs = tuple(sorted(seg_by_fid.get(fid, ())))
        cname_low = cnames[pos].lower()
        acad_only = (bool(segs) and all(s in ("edu", "academic", "физлицо", "personal") for s in segs)) or (
            any(k in cname_low for k in ("academic", "для учебных", "для студентов", "home and student"))
            and "business" not in cname_low
        )
        pmin = _to_int(pmin_col[pos])
        has_price = pmin is not None and pmin > 0
        out[pos] = FamilySignals(
            available_any=bool(avail_col[pos]),
            is_ru_registry=bool(ru_col[pos]),
            has_priced_offer=has_price,
            has_academic_only=acad_only,
            segments=segs,
            terms=tuple(sorted(term_by_fid.get(fid, ()))),
            qtys=tuple(sorted(qty_by_fid.get(fid, ()))),
            license_types=tuple(sorted(lic_by_fid.get(fid, ()))),
            versions=tuple(sorted(ver_by_fid.get(fid, ()))),
        )

    with _cache_lock:
        _cache_ver = index_state.index_version
        _cache_map = out
    return out


def evaluate_candidate_attributes(
    family_idx: int,
    canonical_name: str,
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
) -> CandidateAttributes:
    sig_map = _build_signals_map(index_state)
    sig = sig_map.get(family_idx)
    if sig is None:
        return CandidateAttributes(False, False, False, False, 0.5, 0.0, ())

    attrs = analysis.qu.attrs
    target_seg = attrs.segment or ctx.segment.value

    seg_fit = 0.5
    if sig.segments:
        if target_seg in sig.segments:
            seg_fit = 1.0
        elif target_seg in ("smb", "enterprise", "gov") and sig.has_academic_only:
            seg_fit = 0.0
        elif target_seg == "personal" and not any(s in ("personal", "smb", "home") for s in sig.segments):
            seg_fit = 0.2
    elif target_seg in ("smb", "enterprise", "gov") and sig.has_academic_only:
        seg_fit = 0.0

    bonus = 0.0
    matched: list = []
    if attrs.term_months and attrs.term_months in sig.terms:
        bonus += 0.06
        matched.append("attr:term")
    if attrs.qty_users and attrs.qty_users in sig.qtys:
        bonus += 0.06
        matched.append("attr:qty")
    if attrs.version:
        ver_low = attrs.version.lower()
        if ver_low in canonical_name.lower() or any(ver_low in v for v in sig.versions):
            bonus += 0.08
            matched.append("attr:version")
    if attrs.license_type and any(attrs.license_type in lt for lt in sig.license_types):
        bonus += 0.05
        matched.append("attr:license")
    if "ru_registry" in attrs.modifiers and sig.is_ru_registry:
        bonus += 0.12
        matched.append("attr:ru_registry")

    return CandidateAttributes(
        available_any=sig.available_any,
        has_priced_offer=sig.has_priced_offer,
        is_ru_registry=sig.is_ru_registry,
        academic_only=sig.has_academic_only,
        segment_fit=seg_fit,
        attr_bonus=round(bonus, 4),
        matched_attrs=tuple(matched),
    )