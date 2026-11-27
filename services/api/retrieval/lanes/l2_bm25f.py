"""L2: BM25F по предвзвешенной sparse-матрице (`bm25f.npz` + `bm25f_vocab.pkl`)."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Dict, List, Mapping, Optional, Tuple

import numpy as np

from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis, tokenize
from services.api.nlp.tokenize import lemma_of_token
from services.api.retrieval.lanes import LaneHit

log = logging.getLogger("smart_search.retrieval.l2_bm25f")

_PART_RE = re.compile(r"[-./]")


def _collect_query_weights(analysis: NlpAnalysis) -> Dict[str, float]:
    weights: Dict[str, float] = defaultdict(float)

    def add_tq(tq, w: float) -> None:
        for tok in tq.tokens:
            for form in (tok.text, tok.lemma):
                if form:
                    weights[form] = max(weights[form], w)
            if _PART_RE.search(tok.text):
                joined = _PART_RE.sub("", tok.text)
                if joined:
                    weights[joined] = max(weights[joined], w * 0.95)
                    weights[lemma_of_token(joined)] = max(weights[lemma_of_token(joined)], w * 0.95)
                for p in _PART_RE.split(tok.text):
                    if p:
                        weights[p] = max(weights[p], w * 0.85)
                        weights[lemma_of_token(p)] = max(weights[lemma_of_token(p)], w * 0.85)

    add_tq(analysis.tokenized, 1.0)
    if analysis.hypotheses.applied_query and analysis.hypotheses.applied_query != analysis.normalized.query_norm:
        add_tq(tokenize(analysis.hypotheses.applied_query), 1.05)

    for hyp in analysis.hypotheses.hypotheses[:3]:
        if hyp.confidence >= 0.70:
            add_tq(tokenize(hyp.query), 0.7 * hyp.confidence)

    for rw in analysis.qu.rewrites[:3]:
        add_tq(tokenize(rw), 0.55)

    return dict(weights)


def search_l2_bm25f(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    top_k: int = 150,
) -> List[LaneHit]:
    if not index_state.is_loaded or index_state.bm25f is None or not isinstance(index_state.bm25f_vocab, Mapping):
        return []

    vocab: Mapping[str, int] = index_state.bm25f_vocab.get("vocab") or {}
    if not vocab:
        return []

    term_weights = _collect_query_weights(analysis)
    cols: List[int] = []
    q_weights: List[float] = []
    for term, w in term_weights.items():
        col = vocab.get(term)
        if col is not None:
            cols.append(int(col))
            q_weights.append(float(w))

    if not cols:
        return []

    mat = index_state.bm25f
    try:
        sub = mat[:, cols]
        scores = np.asarray(sub @ np.array(q_weights, dtype=np.float32)).ravel()
    except Exception as exc:  # noqa: BLE001
        log.warning("l2_bm25f matmul failed: %s", exc)
        return []

    nz_idx = np.flatnonzero(scores > 0.0)
    if len(nz_idx) == 0:
        return []

    nz_scores = scores[nz_idx]
    if len(nz_idx) > top_k:
        part = np.argpartition(nz_scores, -top_k)[-top_k:]
        top_pos = nz_idx[part]
        top_scores = nz_scores[part]
    else:
        top_pos = nz_idx
        top_scores = nz_scores

    order = np.argsort(-top_scores)
    return [
        LaneHit(
            family_idx=int(top_pos[i]),
            family_id=index_state.family_id_at(int(top_pos[i])),
            score=round(float(top_scores[i]), 6),
            lane="l2_bm25f",
            matched_fields=("bm25f",),
        )
        for i in order
    ]