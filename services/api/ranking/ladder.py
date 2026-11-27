"""S10: Лестница гарантии >= 4 различных family (инварианты I1 и I5).

Ступени:
  0: основные кандидаты достаточны (>= 4)
  1: снятие мягких ограничений с оставшегося пула кандидатов
  2: гипотезы написания S3 / аналоги `analog_of` из графа
  3: соседние product_type и product_class из families.parquet
  4: статическое «Популярное» по корню категории или `_global` из popular_by_root.json
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from services.api.app.schemas import ClientContext, ProductCard, RelevanceLabel
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis, tokenize
from services.api.ranking.compose import materialize_card
from services.api.ranking.features import FeatureVector
from services.api.ranking.scorer import ScoredCandidate
from services.api.retrieval.filters import evaluate_candidate_attributes
from services.api.retrieval.fusion import FusedCandidate
from services.api.retrieval.lanes.l2_bm25f import search_l2_bm25f


@dataclass
class LadderOutput:
    cards: List[ProductCard]
    fill_rung: int
    soft_null: bool


def _synthetic_scored_from_fid(
    fid: str,
    *,
    score: float,
    label: RelevanceLabel,
    fill_reason: str,
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
) -> Optional[ScoredCandidate]:
    pos = index_state.family_index(fid)
    if pos is None:
        return None
    fam = index_state.family_row(fid)
    if fam is None:
        return None
    cname = str(fam.get("canonical_name") or "")
    attr_info = evaluate_candidate_attributes(pos, cname, analysis, ctx, index_state)
    cand = FusedCandidate(
        family_idx=pos,
        family_id=fid,
        canonical_name=cname,
        vendor=str(fam.get("vendor") or ""),
        product_class=str(fam.get("product_class") or "other"),
        product_type=str(fam.get("product_type") or ""),
        rrf_score=score,
        lane_ranks={"ladder": 1},
        lane_scores={"ladder": score},
        matched_fields=("ladder",),
        attrs=attr_info,
    )
    fv = FeatureVector(
        values={"rel_fused": score, "availability": 1.0 if attr_info.available_any else 0.0, "popularity": score},
        sources={"rel_fused": "rule", "availability": "real", "popularity": "proxy"},
        context_boost=0.0,
        rel_signal=score,
    )
    return ScoredCandidate(
        candidate=cand,
        features=fv,
        score=score,
        relevance_label=label,
        fill_reason=fill_reason,
    )


def apply_guarantee_ladder(
    primary_cards: List[ProductCard],
    remaining_scored: List[ScoredCandidate],
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
    weights: Dict[str, float],
    *,
    min_families: int = 4,
    tau_soft: float = 0.35,
    hard_null: bool = False,
) -> LadderOutput:
    cards: List[ProductCard] = list(primary_cards)
    seen: Set[str] = {c.family_id for c in cards}
    fill_rung = 0

    def try_add_scored(sc: ScoredCandidate, *, force_label: Optional[RelevanceLabel] = None, reason: Optional[str] = None) -> bool:
        if sc.candidate.family_id in seen:
            return False
        if force_label is not None:
            sc.relevance_label = force_label
        if reason:
            sc.fill_reason = reason
        card = materialize_card(sc, analysis, ctx, index_state, weights)
        if card is None:
            return False
        seen.add(card.family_id)
        cards.append(card)
        return True

    # Ступень 1: добор из уже найденного пула S5/S7
    if len(cards) < min_families and remaining_scored:
        fill_rung = max(fill_rung, 1)
        for sc in remaining_scored:
            if len(cards) >= min_families:
                break
            lbl = RelevanceLabel.similar if sc.relevance_label not in (RelevanceLabel.exact, RelevanceLabel.close) else sc.relevance_label
            rsn = "Показано без учёта фильтров" if lbl == RelevanceLabel.similar else sc.fill_reason
            try_add_scored(sc, force_label=lbl, reason=rsn)

    # Ступень 2: аналоги из graph_edges для top-1 или EXTERNAL_PRODUCT + гипотезы S3
    if len(cards) < min_families and index_state.is_loaded:
        fill_rung = max(fill_rung, 2)
        if index_state.graph_edges is not None and len(index_state.graph_edges) > 0:
            seeds = [c.family_id for c in cards[:2]]
            for eh in analysis.qu.entity_hits:
                seeds.extend(eh.ref.family_ids[:3])
            if seeds:
                edges = index_state.graph_edges
                sub = edges[edges["src_family_id"].astype(str).isin(seeds)]
                for row in sub.to_dict("records"):
                    if len(cards) >= min_families:
                        break
                    dst = row.get("dst_family_id")
                    if dst is None or pd.isna(dst) or str(dst) in seen:
                        continue
                    sc = _synthetic_scored_from_fid(
                        str(dst),
                        score=0.25,
                        label=RelevanceLabel.similar,
                        fill_reason="Близкий аналог по каталогу",
                        analysis=analysis,
                        ctx=ctx,
                        index_state=index_state,
                    )
                    if sc is not None:
                        try_add_scored(sc)

        for hyp in analysis.hypotheses.hypotheses[:2]:
            if len(cards) >= min_families:
                break
            hyp_tq = tokenize(hyp.query)
            from services.api.nlp import NlpAnalysis as _NA
            hyp_analysis = _NA(
                normalized=analysis.normalized,
                tokenized=hyp_tq,
                lexicon=analysis.lexicon,
                hypotheses=analysis.hypotheses,
                qu=analysis.qu,
            )
            for lh in search_l2_bm25f(hyp_analysis, index_state, top_k=10):
                if len(cards) >= min_families:
                    break
                sc = _synthetic_scored_from_fid(
                    lh.family_id,
                    score=0.22,
                    label=RelevanceLabel.similar,
                    fill_reason="Похожий запрос",
                    analysis=analysis,
                    ctx=ctx,
                    index_state=index_state,
                )
                if sc is not None:
                    try_add_scored(sc)

    # Ступень 3: соседние товары по product_type / product_class
    if len(cards) < min_families and index_state.is_loaded and index_state.families is not None:
        fill_rung = max(fill_rung, 3)
        df = index_state.families
        target_types = set(analysis.qu.predicted_types) | {c.product_type for c in cards if c.product_type}
        target_classes = set(sorted(analysis.qu.type_probs.keys(), key=lambda k: -analysis.qu.type_probs[k])[:2])

        pool_df = df[df["product_type"].isin(target_types)] if target_types and "product_type" in df.columns else df.iloc[0:0]
        if len(pool_df) < min_families and target_classes and "product_class" in df.columns:
            pool_df = df[df["product_class"].isin(target_classes)]

        if len(pool_df) > 0:
            pool_sorted = pool_df.sort_values(by=["available_any", "pop_prior"], ascending=[False, False]).head(20)
            for fid in pool_sorted["family_id"].astype(str).tolist():
                if len(cards) >= min_families:
                    break
                sc = _synthetic_scored_from_fid(
                    fid,
                    score=0.18,
                    label=RelevanceLabel.similar,
                    fill_reason="Близкий тип продукта",
                    analysis=analysis,
                    ctx=ctx,
                    index_state=index_state,
                )
                if sc is not None:
                    try_add_scored(sc)

    # Ступень 4: популярное по корню / _global / срез families.parquet
    if len(cards) < min_families and index_state.is_loaded:
        fill_rung = max(fill_rung, 4)
        pop_dict = index_state.popular_by_root or {}
        pop_items: List[Dict[str, Any]] = []
        if ctx.page_category_id and str(ctx.page_category_id) in pop_dict:
            pop_items.extend(pop_dict[str(ctx.page_category_id)])
        pop_items.extend(pop_dict.get("_global", []))

        for item in pop_items:
            if len(cards) >= min_families:
                break
            fid = str(item.get("family_id") or "")
            if not fid or fid in seen:
                continue
            sc = _synthetic_scored_from_fid(
                fid,
                score=0.10,
                label=RelevanceLabel.popular,
                fill_reason="Популярное в каталоге",
                analysis=analysis,
                ctx=ctx,
                index_state=index_state,
            )
            if sc is not None:
                try_add_scored(sc)

        if len(cards) < min_families and index_state.families is not None:
            fallback_df = index_state.families.sort_values(
                by=["available_any", "pop_prior"], ascending=[False, False]
            ).head(16)
            for fid in fallback_df["family_id"].astype(str).tolist():
                if len(cards) >= min_families:
                    break
                sc = _synthetic_scored_from_fid(
                    fid,
                    score=0.05,
                    label=RelevanceLabel.popular,
                    fill_reason="Популярное в каталоге",
                    analysis=analysis,
                    ctx=ctx,
                    index_state=index_state,
                )
                if sc is not None:
                    try_add_scored(sc)

    top1_score = cards[0].score if cards else 0.0
    soft_null = hard_null or (fill_rung >= 3) or (top1_score < tau_soft and analysis.qu.confidence < 0.65)
    return LadderOutput(cards=cards, fill_rung=fill_rung, soft_null=soft_null)