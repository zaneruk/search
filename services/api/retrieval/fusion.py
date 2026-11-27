"""S5: Параллельный запуск L1–L5 + RRF Fusion (top-150) + секционные подсказки и фильтрация."""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import yaml

from services.api.app.schemas import ClientContext, NavHit
from services.api.core.degradation import DegradationManager, get_degradation_manager
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.retrieval.filters import CandidateAttributes, evaluate_candidate_attributes
from services.api.retrieval.lanes import LaneHit
from services.api.retrieval.lanes.l1_entity import search_l1_entity
from services.api.retrieval.lanes.l2_bm25f import search_l2_bm25f
from services.api.retrieval.lanes.l3_ngram import search_l3_ngram
from services.api.retrieval.lanes.l4_dense import search_l4_dense
from services.api.retrieval.lanes.l5_type import search_l5_type

log = logging.getLogger("smart_search.retrieval.fusion")

_DEFAULT_LANES: Mapping[str, Dict[str, float]] = {
    "l1_entity": {"top_k": 50, "rrf_weight": 1.5},
    "l2_bm25f": {"top_k": 150, "rrf_weight": 1.0},
    "l3_ngram": {"top_k": 100, "rrf_weight": 0.6},
    "l4_dense": {"top_k": 100, "rrf_weight": 0.8},
    "l5_type": {"top_k": 60, "rrf_weight": 0.7},
}


@dataclass(frozen=True)
class FusedCandidate:
    family_idx: int
    family_id: str
    canonical_name: str
    vendor: str
    product_class: str
    product_type: str
    rrf_score: float
    lane_ranks: Mapping[str, int]
    lane_scores: Mapping[str, float]
    matched_fields: Tuple[str, ...]
    attrs: CandidateAttributes


@dataclass
class RetrievalResult:
    candidates: List[FusedCandidate]
    nav_hits: List[NavHit] = field(default_factory=list)
    out_of_catalog_banner: Optional[str] = None
    hard_null: bool = False
    dense_fallback_l2: bool = False
    lane_counts: Dict[str, int] = field(default_factory=dict)


def _load_retrieval_cfg() -> Tuple[int, int, Mapping[str, Dict[str, Any]]]:
    cfg_path = Path(os.getenv("SEARCH_CONFIG_PATH", "config/search.yaml"))
    pool_size = 150
    rrf_k = 60
    lanes: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _DEFAULT_LANES.items()}
    if cfg_path.is_file():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            rrf_k = int((raw.get("rrf") or {}).get("k", 60))
            ret = raw.get("retrieval") or {}
            pool_size = int(ret.get("candidate_pool_size", 150))
            for lname, lcfg in (ret.get("lanes") or {}).items():
                if isinstance(lcfg, dict):
                    lanes.setdefault(lname, {}).update(lcfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot parse retrieval config: %s", exc)
    return pool_size, rrf_k, lanes


def run_retrieval(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    ctx: Optional[ClientContext] = None,
    disable_dense: bool = False,
    degradation: Optional[DegradationManager] = None,
    query_vector_override: Optional[np.ndarray] = None,
) -> RetrievalResult:
    ctx = ctx or ClientContext()
    if not index_state.is_loaded or index_state.families is None:
        return RetrievalResult(candidates=[], hard_null=True)

    pool_size, rrf_k, lanes_cfg = _load_retrieval_cfg()

    l1_hits, nav_hits, ooc_banner = search_l1_entity(
        analysis, index_state, top_k=int(lanes_cfg["l1_entity"].get("top_k", 50))
    )
    l2_hits = search_l2_bm25f(
        analysis, index_state, top_k=int(lanes_cfg["l2_bm25f"].get("top_k", 150))
    )
    l3_hits = search_l3_ngram(
        analysis,
        index_state,
        top_k=int(lanes_cfg["l3_ngram"].get("top_k", 100)),
        min_similarity=float(lanes_cfg["l3_ngram"].get("min_similarity", 0.35)),
    )
    l4_hits, dense_fb = search_l4_dense(
        analysis,
        index_state,
        top_k=int(lanes_cfg["l4_dense"].get("top_k", 100)),
        min_cosine=float(lanes_cfg["l4_dense"].get("min_cosine", 0.30)),
        disable_requested=disable_dense,
        degradation=degradation or get_degradation_manager(),
        query_vector_override=query_vector_override,
    )
    l5_hits = search_l5_type(
        analysis, index_state, top_k=int(lanes_cfg["l5_type"].get("top_k", 60))
    )

    all_lanes: List[Tuple[str, List[LaneHit]]] = [
        ("l1_entity", l1_hits),
        ("l2_bm25f", l2_hits),
        ("l3_ngram", l3_hits),
        ("l4_dense", l4_hits),
        ("l5_type", l5_hits),
    ]
    lane_counts = {name: len(hits) for name, hits in all_lanes}

    # hard_null: 0 кандидатов от поисковых полос до type-injection и лестницы
    hard_null = (len(l1_hits) + len(l2_hits) + len(l3_hits) + len(l4_hits)) == 0

    fused_scores: Dict[int, float] = defaultdict(float)
    ranks_map: Dict[int, Dict[str, int]] = defaultdict(dict)
    scores_map: Dict[int, Dict[str, float]] = defaultdict(dict)
    fields_map: Dict[int, List[str]] = defaultdict(list)

    for lname, hits in all_lanes:
        weight = float(lanes_cfg[lname].get("rrf_weight", 1.0))
        for rank_1, h in enumerate(hits, start=1):
            fused_scores[h.family_idx] += weight / (rrf_k + rank_1)
            ranks_map[h.family_idx][lname] = rank_1
            scores_map[h.family_idx][lname] = h.score
            for f in h.matched_fields:
                if f not in fields_map[h.family_idx]:
                    fields_map[h.family_idx].append(f)

    if not fused_scores:
        return RetrievalResult(
            candidates=[],
            nav_hits=nav_hits,
            out_of_catalog_banner=ooc_banner,
            hard_null=True,
            dense_fallback_l2=dense_fb,
            lane_counts=lane_counts,
        )

    f_df = index_state.families
    cnames = f_df["canonical_name"].astype(str).tolist()
    vendors = f_df["vendor"].fillna("").astype(str).tolist()
    pclasses = f_df["product_class"].fillna("other").astype(str).tolist()
    ptypes = f_df["product_type"].fillna("").astype(str).tolist()

    top_positions = sorted(fused_scores.keys(), key=lambda pos: (-fused_scores[pos], pos))[:pool_size]
    candidates: List[FusedCandidate] = []
    for pos in top_positions:
        cname = cnames[pos]
        attr_info = evaluate_candidate_attributes(pos, cname, analysis, ctx, index_state)
        merged_fields = list(fields_map[pos])
        for ma in attr_info.matched_attrs:
            if ma not in merged_fields:
                merged_fields.append(ma)
        candidates.append(
            FusedCandidate(
                family_idx=pos,
                family_id=index_state.family_id_at(pos),
                canonical_name=cname,
                vendor=vendors[pos],
                product_class=pclasses[pos],
                product_type=ptypes[pos],
                rrf_score=round(fused_scores[pos], 6),
                lane_ranks=ranks_map[pos],
                lane_scores=scores_map[pos],
                matched_fields=tuple(merged_fields),
                attrs=attr_info,
            )
        )

    return RetrievalResult(
        candidates=candidates,
        nav_hits=nav_hits,
        out_of_catalog_banner=ooc_banner,
        hard_null=hard_null,
        dense_fallback_l2=dense_fb,
        lane_counts=lane_counts,
    )