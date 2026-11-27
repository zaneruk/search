"""L3: Char-3gram fuzzy поиск по `char3gram.npz` + `char3gram_vocab.pkl` (char_wb, L2 cosine)."""
from __future__ import annotations

import logging
from collections import Counter
from typing import Dict, List, Mapping

import numpy as np

from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.retrieval.lanes import LaneHit

log = logging.getLogger("smart_search.retrieval.l3_ngram")


def _char_wb_ngrams(text: str, n: int = 3) -> List[str]:
    out: List[str] = []
    for word in (text or "").lower().replace("ё", "е").split():
        padded = f" {word} "
        if len(padded) < n:
            continue
        for i in range(len(padded) - n + 1):
            out.append(padded[i : i + n])
    return out


def search_l3_ngram(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    top_k: int = 100,
    min_similarity: float = 0.35,
) -> List[LaneHit]:
    if not index_state.is_loaded or index_state.char3gram is None or not isinstance(index_state.char3gram_vocab, Mapping):
        return []

    vocab: Mapping[str, int] = index_state.char3gram_vocab.get("vocabulary") or {}
    idf = index_state.char3gram_vocab.get("idf")
    if not vocab or idf is None:
        return []

    queries = [analysis.normalized.query_norm]
    if analysis.hypotheses.applied_query and analysis.hypotheses.applied_query not in queries:
        queries.append(analysis.hypotheses.applied_query)
    elif analysis.hypotheses.did_you_mean and analysis.hypotheses.did_you_mean not in queries:
        queries.append(analysis.hypotheses.did_you_mean)

    best_scores = None
    mat = index_state.char3gram
    idf_arr = np.asarray(idf, dtype=np.float32)

    for q in queries:
        if not q or len(q) < 2:
            continue
        counts = Counter(_char_wb_ngrams(q, n=3))
        cols: List[int] = []
        vals: List[float] = []
        for ng, cnt in counts.items():
            col = vocab.get(ng)
            if col is not None and col < len(idf_arr):
                cols.append(int(col))
                vals.append(float(cnt) * float(idf_arr[col]))
        if not cols:
            continue
        q_vec = np.asarray(vals, dtype=np.float32)
        norm = float(np.linalg.norm(q_vec))
        if norm <= 1e-12:
            continue
        q_vec /= norm
        try:
            sim = np.asarray(mat[:, cols] @ q_vec).ravel()
        except Exception as exc:  # noqa: BLE001
            log.warning("l3_ngram matmul failed: %s", exc)
            continue
        best_scores = sim if best_scores is None else np.maximum(best_scores, sim)

    if best_scores is None:
        return []

    cand_idx = np.flatnonzero(best_scores >= float(min_similarity))
    if len(cand_idx) == 0:
        return []

    cand_scores = best_scores[cand_idx]
    if len(cand_idx) > top_k:
        part = np.argpartition(cand_scores, -top_k)[-top_k:]
        top_pos = cand_idx[part]
        top_scores = cand_scores[part]
    else:
        top_pos = cand_idx
        top_scores = cand_scores

    order = np.argsort(-top_scores)
    return [
        LaneHit(
            family_idx=int(top_pos[i]),
            family_id=index_state.family_id_at(int(top_pos[i])),
            score=round(float(top_scores[i]), 6),
            lane="l3_ngram",
            matched_fields=("char3gram",),
        )
        for i in order
    ]