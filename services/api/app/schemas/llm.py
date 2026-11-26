from typing import Literal, Optional, Dict, List, Any
from pydantic import BaseModel, Field
from .common import RootCause, TicketType
from .search import IntentChip
from .admin import OverlayRulePayload
class QuLlmOutput(BaseModel):
    intent: str
    type_probs: Dict[str, float]
    entity_ids: List[str] = Field(default_factory=list)  # только из переданного closed-set или []
    attr_filters: Dict[str, Any] = Field(default_factory=dict)
    rewrites: List[str] = Field(default_factory=list, max_length=3)
    clarification_chips: List[IntentChip] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


class EnrichFamilyOutput(BaseModel):
    canonical_name_ru: str
    canonical_name_en: str
    aliases: List[str] = Field(default_factory=list, max_length=12)
    typical_queries: List[str] = Field(default_factory=list, max_length=6)
    use_cases: List[str] = Field(default_factory=list, max_length=4)
    buyer_roles: List[str] = Field(default_factory=list, max_length=4)
    analogs: List[str] = Field(default_factory=list, max_length=5)
    complements: List[str] = Field(default_factory=list, max_length=5)


class AgentDiagnoseOutput(BaseModel):
    root_cause: RootCause
    ticket_type: TicketType
    target_role: Literal["content", "product", "ecom"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    proposed_patch: Optional[OverlayRulePayload] = None


class JudgeOutput(BaseModel):
    grade: int = Field(ge=0, le=3)
    reason: str