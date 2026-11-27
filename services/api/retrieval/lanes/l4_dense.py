"""L4: Dense cosine поиск по `embeddings.npy` (BAAI/bge-m3, 1024-dim, L2-нормированные).

* Модель загружается лениво (потокобезопасно) и проверяется через `DegradationManager`
  (chaos-тумблер `force_disable_dense` + `dense_breaker` -> режим L2 при падении).
* Кодированные вектора запросов кэшируются в памяти (LRU 2048).
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, List, Optional, Tuple

import numpy as np

from services.api.core.degradation import DegradationManager, get_degradation_manager
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis
from services.api.retrieval.lanes import LaneHit

log = logging.getLogger("smart_search.retrieval.l4_dense")

_model_lock = threading.Lock()
_models: dict = {}
_vec_cache: "OrderedDict[Tuple[str, str], np.ndarray]" = OrderedDict()
_vec_lock = threading.Lock()
_MAX_VEC_CACHE = 2048


def _load_encoder(model_name: str) -> Any:
    with _model_lock:
        if model_name in _models:
            return _models[model_name]
        from sentence_transformers import SentenceTransformer

        device = os.getenv("DENSE_DEVICE", "cpu").strip() or "cpu"
        log.info("loading dense query encoder %s on %s ...", model_name, device)
        model = SentenceTransformer(model_name, device=device)
        model.max_seq_length = 128
        _models[model_name] = model
        return model


def encode_query(text: str, model_name: str) -> Optional[np.ndarray]:
    key = (model_name, text)
    with _vec_lock:
        cached = _vec_cache.get(key)
        if cached is not None:
            _vec_cache.move_to_end(key)
            return cached

    model = _load_encoder(model_name)
    prefix = "query: " if "e5" in model_name.lower() else ""
    vec = model.encode(
        [f"{prefix}{text}"],
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )[0]
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 1e-12:
        vec = vec / norm

    with _vec_lock:
        _vec_cache[key] = vec
        _vec_cache.move_to_end(key)
        while len(_vec_cache) > _MAX_VEC_CACHE:
            _vec_cache.popitem(last=False)
    return vec


def search_l4_dense(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    top_k: int = 100,
    min_cosine: float = 0.30,
    disable_requested: bool = False,
    degradation: Optional[DegradationManager] = None,
    query_vector_override: Optional[np.ndarray] = None,
) -> Tuple[List[LaneHit], bool]:
    """Возвращает (hits, fallback_to_l2)."""
    if not index_state.is_loaded or not index_state.has_dense or index_state.embeddings is None:
        return [], False

    deg = degradation or get_degradation_manager()
    if not deg.allow_dense(disable_requested=disable_requested):
        return [], True

    query_text = analysis.hypotheses.applied_query or analysis.normalized.query_norm
    if not query_text:
        return [], False

    if query_vector_override is not None:
        q_vec = np.asarray(query_vector_override, dtype=np.float32)
    else:
        if not deg.dense_call_allowed():
            return [], True
        model_name = str(index_state.manifest.get("embedding_model") or "BAAI/bge-m3")
        try:
            q_vec = encode_query(query_text, model_name)
            if q_vec is None or q_vec.shape[0] != index_state.embeddings.shape[1]:
                deg.record_dense(False)
                return [], True
            deg.record_dense(True)
        except Exception as exc:  # noqa: BLE001
            log.warning("l4_dense encoder failed, degrading to L2: %s", exc)
            deg.record_dense(False)
            return [], True

    sims = np.asarray(index_state.embeddings @ q_vec, dtype=np.float32).ravel()
    cand_idx = np.flatnonzero(sims >= float(min_cosine))
    if len(cand_idx) == 0:
        return [], False

    cand_scores = sims[cand_idx]
    if len(cand_idx) > top_k:
        part = np.argpartition(cand_scores, -top_k)[-top_k:]
        top_pos = cand_idx[part]
        top_scores = cand_scores[part]
    else:
        top_pos = cand_idx
        top_scores = cand_scores

    order = np.argsort(-top_scores)
    return (
        [
            LaneHit(
                family_idx=int(top_pos[i]),
                family_id=index_state.family_id_at(int(top_pos[i])),
                score=round(float(top_scores[i]), 6),
                lane="l4_dense",
                matched_fields=("dense_embedding",),
            )
            for i in order
        ],
        False,
    )