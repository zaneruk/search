"""Фасад модуля Retrieval (шаг S5: L1–L5 + RRF Fusion)."""
from services.api.retrieval.filters import CandidateAttributes, evaluate_candidate_attributes
from services.api.retrieval.fusion import FusedCandidate, RetrievalResult, run_retrieval
from services.api.retrieval.lanes import LaneHit

__all__ = [
    "CandidateAttributes",
    "FusedCandidate",
    "LaneHit",
    "RetrievalResult",
    "evaluate_candidate_attributes",
    "run_retrieval",
]