"""S7: Линейное ранжирование + бизнес-полоса delta для маржи/акций (A.9) + правила Overlay."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

import yaml

from services.api.app.schemas import ClientContext, RelevanceLabel
from services.api.core.index_state import IndexState
from services.api.core.overlay import OverlaySnapshot
from services.api.nlp import NlpAnalysis
from services.api.ranking.features import FeatureVector, extract_features
from services.api.retrieval.fusion import FusedCandidate

log = logging.getLogger("smart_search.ranking.scorer")

_BUSINESS_FEATURES = frozenset({"margin_proxy", "promo"})


@dataclass
class ScoredCandidate:
    candidate: FusedCandidate
    features: FeatureVector
    score: float
    relevance_label: RelevanceLabel
    pinned: bool = False
    fill_reason: Optional[str] = None


def _load_delta_business() -> float:
    cfg_path = Path(os.getenv("SEARCH_CONFIG_PATH", "config/search.yaml"))
    if cfg_path.is_file():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            return float((raw.get("ranking") or {}).get("delta_business", 0.08))
        except Exception:  # noqa: BLE001
            pass
    return 0.08


def _match_overlay_fids(overlay: Optional[OverlaySnapshot], kind: str, q_norm: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if overlay is None or overlay.is_empty:
        return out
    rules = overlay.rules(kind)
    for key in (q_norm, "*"):
        for item in rules.get(key, ()):
            val = item.value
            fids = val.get("family_ids") or ([val["family_id"]] if val.get("family_id") else [])
            delta = float(val.get("delta", 0.35 if kind == "boost" else (-0.45 if kind == "demote" else 1.0)))
            for fid in fids:
                out[str(fid)] = delta
    return out


def score_candidates(
    candidates: List[FusedCandidate],
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
    *,
    overlay: Optional[OverlaySnapshot] = None,
    allow_personalization: bool = True,
) -> List[ScoredCandidate]:
    if not candidates:
        return []

    weights: Dict[str, float] = dict(overlay.ranking_weights) if overlay is not None else {
        "rel_fused": 0.34, "exact_name_match": 0.16, "brand_match": 0.08,
        "availability": 0.10, "popularity": 0.08, "margin_proxy": 0.05,
        "promo": 0.04, "version_recency": 0.06, "segment_fit": 0.09,
    }
    delta_band = _load_delta_business()
    max_rrf = max(c.rrf_score for c in candidates)
    q_norm = analysis.hypotheses.applied_query or analysis.normalized.query_norm

    pins = _match_overlay_fids(overlay, "pin", q_norm)
    boosts = _match_overlay_fids(overlay, "boost", q_norm)
    demotes = _match_overlay_fids(overlay, "demote", q_norm)

    fvs: List[Tuple[FusedCandidate, FeatureVector]] = []
    for c in candidates:
        fam_row = index_state.family_row(c.family_id)
        fv = extract_features(
            c,
            max_rrf=max_rrf,
            analysis=analysis,
            ctx=ctx,
            index_state=index_state,
            family_row=fam_row,
            allow_personalization=allow_personalization,
        )
        fvs.append((c, fv))

    best_rel = max(fv.rel_signal for _, fv in fvs)

    scored: List[ScoredCandidate] = []
    for c, fv in fvs:
        in_band = (best_rel - fv.rel_signal) <= delta_band
        total = 0.0
        for fname, val in fv.values.items():
            w = float(weights.get(fname, 0.0))
            if fname in _BUSINESS_FEATURES and not in_band:
                w *= 0.1  # вне полосы релевантности маржа/акция не ломают выдачу (A.9)
            total += val * w

        total += fv.context_boost
        if not c.attrs.has_priced_offer:
            total -= 0.03  # мягкий штраф за on_request

        pinned = c.family_id in pins
        if pinned:
            total += 10.0 + pins[c.family_id]
        if c.family_id in boosts:
            total += boosts[c.family_id]
        if c.family_id in demotes:
            total -= abs(demotes[c.family_id])

        # Метка релевантности
        if pinned or fv.values["exact_name_match"] >= 0.72 or ("l1_entity" in c.lane_ranks and c.lane_ranks["l1_entity"] <= 2):
            label = RelevanceLabel.exact
        elif fv.rel_signal >= 0.45 or ("l2_bm25f" in c.lane_ranks) or ("l4_dense" in c.lane_ranks):
            label = RelevanceLabel.close
        else:
            label = RelevanceLabel.similar

        scored.append(
            ScoredCandidate(
                candidate=c,
                features=fv,
                score=round(total, 6),
                relevance_label=label,
                pinned=pinned,
                fill_reason="Близкий тип продукта" if label == RelevanceLabel.similar else None,
            )
        )

    scored.sort(key=lambda s: (-s.score, s.candidate.family_id))
    return scored