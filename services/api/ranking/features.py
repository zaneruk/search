"""S6 + S7: Извлечение признаков ранжирования и контекстного буста (±15%)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from services.api.app.schemas import ClientContext, FeatureContribution
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.nlp.config import basic_norm
from services.api.retrieval.fusion import FusedCandidate

_YEAR_RE = re.compile(r"\b(201\d|202\d)\b")


@dataclass(frozen=True)
class FeatureVector:
    values: Dict[str, float]
    sources: Dict[str, str]
    context_boost: float
    rel_signal: float


def _safe_float(val, default: float = 0.0) -> float:
    if val is None or pd.isna(val):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _version_recency(canonical_name: str, attrs_version: Optional[str]) -> float:
    years = [int(y) for y in _YEAR_RE.findall(canonical_name)]
    if not years:
        return 0.5
    latest = max(years)
    if attrs_version and attrs_version in canonical_name:
        return 1.0
    # 2024..2026 -> ~1.0, 2019 -> ~0.3
    age = max(0, 2026 - latest)
    return round(max(0.2, min(1.0, math.exp(-0.23 * age))), 4)


def _margin_proxy(price_min_minor: Optional[int], product_class: str) -> float:
    if price_min_minor is None or price_min_minor <= 0:
        return 0.0
    rub = price_min_minor / 100.0
    tier = 0.2 if rub < 3000 else (0.5 if rub < 15000 else (0.8 if rub < 70000 else 1.0))
    if product_class in ("cloud_service", "subscription", "software"):
        tier = min(1.0, tier + 0.1)
    return round(tier, 4)


def extract_features(
    cand: FusedCandidate,
    *,
    max_rrf: float,
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
    family_row: Optional[Dict],
    allow_personalization: bool = True,
) -> FeatureVector:
    q_norm = analysis.hypotheses.applied_query or analysis.normalized.query_norm
    q_tokens = [t for t in analysis.tokenized.lemmas if len(t) >= 2]
    cname_norm = basic_norm(cand.canonical_name)
    vendor_norm = basic_norm(cand.vendor)

    rel_fused = min(1.0, cand.rrf_score / max(1e-6, max_rrf))

    # exact_name_match
    if q_norm and q_norm == cname_norm:
        exact_name = 1.0
    elif q_norm and q_norm in cname_norm:
        exact_name = 0.85
    elif q_tokens:
        hits = sum(1 for t in q_tokens if t in cname_norm)
        exact_name = hits / len(q_tokens)
    else:
        exact_name = 0.0
    if "l1_entity" in cand.lane_ranks and cand.lane_ranks["l1_entity"] <= 3:
        exact_name = max(exact_name, 0.75)

    # brand_match
    brand_match = 0.0
    if vendor_norm:
        if vendor_norm in q_norm or any(bt in q_norm for bt in (vendor_norm.split()[0],) if len(bt) >= 3):
            brand_match = 1.0
        elif any(eh.ref.kind == "brand" and basic_norm(eh.ref.canonical_name) == vendor_norm for eh in analysis.qu.entity_hits):
            brand_match = 1.0

    # Intent alignment boost/penalty: если QU предсказал product_class, совпадение усиливает релевантность
    p_cls = analysis.qu.type_probs.get(cand.product_class, 0.0)
    intent_align = min(1.0, p_cls * 1.15) if analysis.qu.type_probs else 0.5

    fam = family_row or {}
    availability = 1.0 if cand.attrs.available_any else 0.0
    pop_raw = _safe_float(fam.get("pop_prior"), 0.0)
    popularity = min(1.0, math.log1p(max(0.0, pop_raw)) / 2.5)

    pmin_val = fam.get("price_min_minor")
    pmin = int(float(pmin_val)) if pmin_val is not None and not pd.isna(pmin_val) else None
    margin_proxy = _margin_proxy(pmin, cand.product_class)

    disc_val = _safe_float(fam.get("discount_best"), 0.0)
    promo = 1.0 if 0.0 < disc_val <= 70.0 else 0.0

    ver_recency = _version_recency(cand.canonical_name, analysis.qu.attrs.version)
    segment_fit = cand.attrs.segment_fit

    # S6: Контекстный буст/штраф, строго зажатый в [-0.15, +0.15]
    ctx_boost = 0.0
    if allow_personalization:
        if ctx.page_category_id and str(fam.get("category_id") or "") == ctx.page_category_id:
            ctx_boost += 0.10
        if cand.family_id in ctx.clicked_family_ids:
            ctx_boost += 0.06
        if ctx.segment.value in ("smb", "enterprise", "gov") and cand.attrs.academic_only:
            ctx_boost -= 0.14
        if ctx.segment.value == "gov" and cand.attrs.is_ru_registry:
            ctx_boost += 0.10
        if ctx.recent_queries:
            last_q = basic_norm(ctx.recent_queries[-1])
            if any(w in cname_norm for w in last_q.split() if len(w) >= 4):
                ctx_boost += 0.04
        ctx_boost = max(-0.15, min(0.15, ctx_boost))

    values = {
        "rel_fused": round(min(1.0, rel_fused * 0.75 + intent_align * 0.25 + cand.attrs.attr_bonus), 4),
        "exact_name_match": round(exact_name, 4),
        "brand_match": round(brand_match, 4),
        "availability": availability,
        "popularity": round(popularity, 4),
        "margin_proxy": margin_proxy,
        "promo": promo,
        "version_recency": ver_recency,
        "segment_fit": round(segment_fit, 4),
    }
    sources = {
        "rel_fused": "rule",
        "exact_name_match": "rule",
        "brand_match": "rule",
        "availability": "real",
        "popularity": "proxy",
        "margin_proxy": "proxy",
        "promo": "real",
        "version_recency": "rule",
        "segment_fit": "rule",
    }
    rel_signal = values["rel_fused"] * 0.65 + values["exact_name_match"] * 0.25 + values["brand_match"] * 0.10
    return FeatureVector(values=values, sources=sources, context_boost=round(ctx_boost, 4), rel_signal=round(rel_signal, 4))


def build_feature_contributions(fv: FeatureVector, weights: Dict[str, float]) -> List[FeatureContribution]:
    items = []
    for fname, val in fv.values.items():
        w = float(weights.get(fname, 0.0))
        items.append(
            FeatureContribution(
                feature=fname,
                value=round(val, 4),
                contribution=round(val * w, 4),
                source=fv.sources.get(fname, "rule"),
            )
        )
    if abs(fv.context_boost) > 1e-6:
        items.append(
            FeatureContribution(
                feature="context_s6",
                value=fv.context_boost,
                contribution=fv.context_boost,
                source="rule",
            )
        )
    items.sort(key=lambda x: -abs(x.contribution))
    return items