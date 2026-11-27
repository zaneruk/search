"""Общие типы кандидатов полос L1–L5."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class LaneHit:
    family_idx: int
    family_id: str
    score: float
    lane: str
    matched_fields: Tuple[str, ...] = field(default_factory=tuple)