from typing import Literal, Optional, Dict, List, Any
from pydantic import BaseModel, Field
from .common import DataTier, RootCause, TicketType, EventType
class EventIn(BaseModel):
    event_type: EventType
    session_id: str
    trace_id: Optional[str] = None
    source: Literal["live", "simulated", "gold_replay"] = "live"
    payload: Dict[str, Any] = Field(default_factory=dict)


class OverlayRulePayload(BaseModel):
    kind: Literal["synonym", "alias", "pin", "demote", "boost", "catalog_patch"]
    key: str
    value: Dict[str, Any]


class TicketItem(BaseModel):
    ticket_id: str
    ticket_type: TicketType
    root_cause: RootCause
    target_role: Literal["content", "product", "ecom"]
    cluster_key: str
    affected_queries: List[str]
    frequency: int
    estimated_monthly_rub: int
    estimation_tier: DataTier = DataTier.modeled
    confidence: float
    summary: str
    evidence: List[str] = Field(default_factory=list)
    proposed_patch: Optional[OverlayRulePayload] = None
    dry_run_before_hit4: Optional[float] = None
    dry_run_after_hit4: Optional[float] = None
    status: Literal["open", "applied", "rejected", "rolled_back"]
    applied_config_version: Optional[int] = None
    created_at: str


class ApplyTicketResponse(BaseModel):
    ticket_id: str
    gold10_passed: bool
    gold10_details: List[Dict[str, Any]]
    new_config_version: int
    replay_results: List[Dict[str, Any]]


class ImpactAssumptions(BaseModel):
    q_month: int = 15000
    f_b: float = 0.30
    f_n: Optional[float] = None  # если None — берётся из последнего eval
    cvr_s: float = 0.075
    kappa: float = 0.30
    aov_rub: Optional[float] = None  # если None — медиана/среднее фида
    margin: float = 0.20
    p_exit: float = 0.40


class ImpactScenarioOut(BaseModel):
    scenario: Literal["pessimistic", "base", "optimistic"]
    recovered_sessions_month: float
    delta_orders_month: float
    delta_revenue_rub_month: float
    delta_profit_rub_month: float
    lost_orders_before_month: float
    lost_orders_after_month: float
    exits_before_month: float
    exits_after_month: float
    data_tier: DataTier = DataTier.modeled


class ImpactReport(BaseModel):
    assumptions_used: ImpactAssumptions
    scenarios: List[ImpactScenarioOut]
    data_tier: DataTier = DataTier.modeled
    note: str = "Сценарный расчёт на допущениях, не измерение реального трафика"