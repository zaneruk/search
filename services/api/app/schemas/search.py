from typing import Literal, Optional, Dict, List, Any
from pydantic import BaseModel, Field
from .common import DataTier, DegradationLevel, PriceState, RelevanceLabel, ProductClass, Segment, StepId
class ClientContext(BaseModel):
    persona_id: Optional[str] = None
    segment: Segment = Segment.smb
    page_category_id: Optional[str] = None
    recent_queries: List[str] = Field(default_factory=list)
    clicked_family_ids: List[str] = Field(default_factory=list)
    purchased_family_ids: List[str] = Field(default_factory=list)


class SearchOptions(BaseModel):
    mode: Literal["smart", "legacy"] = "smart"
    llm_enabled: bool = True
    disable_dense: bool = False
    limit: int = Field(default=20, ge=4, le=50)
    include_trace: bool = True


class SearchRequest(BaseModel):
    query: str = Field(..., max_length=300)
    session_id: str = "ss_demo"
    context: ClientContext = Field(default_factory=ClientContext)
    options: SearchOptions = Field(default_factory=SearchOptions)


class Badge(BaseModel):
    type: str  # "promo" | "price_advantage" | "available" | "on_request" | "ru_registry"
    label: str
    basis: Optional[Dict[str, Any]] = None


class OfferView(BaseModel):
    offer_id: str
    name_raw: str
    available: bool
    price_state: PriceState
    price_minor: Optional[int] = None      # null если on_request
    oldprice_minor: Optional[int] = None
    discount_pct: Optional[float] = None
    term_months: Optional[int] = None
    qty_users: Optional[int] = None
    license_type: Optional[str] = None
    segment_fit: Optional[str] = None
    url: Optional[str] = None


class FeatureContribution(BaseModel):
    feature: str
    value: float
    contribution: float
    source: str  # "real" | "proxy" | "simulated" | "rule"


class ProductCard(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    product_class: ProductClass
    product_type: str
    image_url: Optional[str] = None
    representative: OfferView
    n_variants: int
    variant_axes: Dict[str, List[str]] = Field(default_factory=dict)
    price_min_minor: Optional[int] = None
    badges: List[Badge] = Field(default_factory=list)
    relevance_label: RelevanceLabel
    fill_reason: Optional[str] = None
    matched_fields: List[str] = Field(default_factory=list)
    feature_contributions: List[FeatureContribution] = Field(default_factory=list)
    score: float


class CrossSellItem(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    relation_type: str  # "complements" | "upsell_to" | "analog_of"
    source: str         # "vendor_evidence" | "catalog_structure" | "llm_validated" | "co_click"
    evidence_label: Optional[str] = None
    evidence_url: Optional[str] = None
    price_min_minor: Optional[int] = None
    available: bool


class SearchSection(BaseModel):
    section_id: str
    title: str
    intent_class: Optional[ProductClass] = None
    empty_reason: Optional[str] = None
    items: List[ProductCard] = Field(default_factory=list)


class NavHit(BaseModel):
    entity_id: str
    kind: Literal["CATEGORY", "CONTENT", "EXTERNAL_PRODUCT"]
    title: str
    url: Optional[str] = None
    note: Optional[str] = None


class IntentChip(BaseModel):
    label: str
    query_rewrite: str
    intent_hint: Optional[str] = None


class TraceStep(BaseModel):
    id: StepId
    title: str
    status: Literal["ok", "skipped", "fallback", "error"]
    ms: float
    cpu_ms: float
    summary: str
    decision: Optional[str] = None
    fallback_used: bool = False
    data: Dict[str, Any] = Field(default_factory=dict)


class CostMeter(BaseModel):
    llm_calls: int
    tokens_in: int
    tokens_cached: int
    tokens_out: int
    cpu_ms_total: float
    cost_micro_rub: int
    data_tier: DataTier = DataTier.derived


class SearchResponse(BaseModel):
    contract_version: str = "2.0.0-mvp"
    trace_id: str
    query_raw: str
    query_norm: str
    applied_query: Optional[str] = None
    did_you_mean: Optional[str] = None
    mode: Literal["smart", "legacy"]
    degradation_level: DegradationLevel
    config_version: int
    index_version: str
    hard_null: bool
    soft_null: bool
    fill_rung: int
    out_of_catalog_banner: Optional[str] = None
    nav_hits: List[NavHit] = Field(default_factory=list)
    intent_chips: List[IntentChip] = Field(default_factory=list)
    sections: List[SearchSection] = Field(default_factory=list)
    cross_sell: List[CrossSellItem] = Field(default_factory=list)
    related_queries: List[str] = Field(default_factory=list)
    trace: List[TraceStep] = Field(default_factory=list)
    cost_meter: CostMeter


class SuggestCompletion(BaseModel):
    text: str
    kind: Literal["brand", "product", "alias", "category", "seed_query", "layout_fix"]
    score: float


class SuggestProductHit(BaseModel):
    family_id: str
    canonical_name: str
    vendor: str
    image_url: Optional[str] = None
    price_min_minor: Optional[int] = None
    available: bool
    has_promo: bool


class SuggestResponse(BaseModel):
    contract_version: str = "2.0.0-mvp"
    prefix_raw: str
    layout_corrected: Optional[str] = None
    completions: List[SuggestCompletion] = Field(default_factory=list)
    products: List[SuggestProductHit] = Field(default_factory=list)
    related_queries: List[str] = Field(default_factory=list)
    intent_chips: List[IntentChip] = Field(default_factory=list)
    latency_ms: float