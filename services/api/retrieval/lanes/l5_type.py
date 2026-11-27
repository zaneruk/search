"""L5: Type-injection — популярные и доступные family по предсказанным product_type и product_class."""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.retrieval.lanes import LaneHit


@dataclass(frozen=True)
class _TypeCache:
    index_version: str
    by_type: Mapping[str, Tuple[int, ...]]
    by_class: Mapping[str, Tuple[int, ...]]


_cache_lock = threading.Lock()
_cache: Optional[_TypeCache] = None


def _get_type_cache(index_state: IndexState) -> _TypeCache:
    global _cache
    with _cache_lock:
        if _cache is not None and _cache.index_version == index_state.index_version:
            return _cache

    df = index_state.families
    n = len(df)
    avail = df["available_any"].fillna(False).astype(bool).tolist() if "available_any" in df.columns else [False] * n
    pop = df["pop_prior"].fillna(0.0).astype(float).tolist() if "pop_prior" in df.columns else [0.0] * n
    ptypes = df["product_type"].fillna("").astype(str).tolist() if "product_type" in df.columns else [""] * n
    pclasses = df["product_class"].fillna("other").astype(str).tolist() if "product_class" in df.columns else ["other"] * n

    order = sorted(range(n), key=lambda i: (bool(avail[i]), float(pop[i] or 0.0)), reverse=True)
    by_type: Dict[str, List[int]] = defaultdict(list)
    by_class: Dict[str, List[int]] = defaultdict(list)
    for pos in order:
        pt = ptypes[pos].strip()
        if pt and len(by_type[pt]) < 120:
            by_type[pt].append(pos)
        pc = pclasses[pos].strip() or "other"
        if len(by_class[pc]) < 150:
            by_class[pc].append(pos)

    built = _TypeCache(
        index_version=index_state.index_version,
        by_type={k: tuple(v) for k, v in by_type.items()},
        by_class={k: tuple(v) for k, v in by_class.items()},
    )
    with _cache_lock:
        _cache = built
    return built


def search_l5_type(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    top_k: int = 60,
) -> List[LaneHit]:
    if not index_state.is_loaded or index_state.families is None:
        return []

    qu = analysis.qu
    if not qu.predicted_types and not qu.type_probs:
        return []

    tc = _get_type_cache(index_state)
    scores: Dict[int, float] = {}

    # 1) Точные предсказанные product_type
    for rank, pt in enumerate(qu.predicted_types[:4]):
        w_pt = 1.0 - 0.1 * rank
        for pos in tc.by_type.get(pt, ())[:25]:
            scores[pos] = max(scores.get(pos, 0.0), w_pt)

    # 2) Инъекция по классам с вероятностью >= 0.10 (даёт разнообразие при мультиинтенте вроде «сервер»)
    for cls, prob in sorted(qu.type_probs.items(), key=lambda kv: -kv[1]):
        if prob < 0.10:
            continue
        take = max(8, int(round(top_k * prob)))
        for idx, pos in enumerate(tc.by_class.get(cls, ())[:take]):
            sc = float(prob) * (1.0 - idx * 0.005)
            scores[pos] = max(scores.get(pos, 0.0), sc)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    return [
        LaneHit(
            family_idx=pos,
            family_id=index_state.family_id_at(pos),
            score=round(sc, 6),
            lane="l5_type",
            matched_fields=("product_type",),
        )
        for pos, sc in ranked
    ]