/**
 * Единые TypeScript-контракты SPA.
 * Синхронизированы с CONTRACTS.md (B.4, B.8, B.9) и роутерами admin_*.py.
 */

// --- Базовые Enum (B.4.1) ---

export type DataTier = 'real' | 'derived' | 'judged' | 'simulated' | 'modeled';

export type DegradationLevel = 'L0' | 'L1' | 'L2' | 'L3';

export type PriceState = 'priced' | 'on_request';

export type RelevanceLabel = 'exact' | 'close' | 'similar' | 'popular';

export type ProductClass =
  | 'software'
  | 'hardware'
  | 'cloud_service'
  | 'subscription'
  | 'certificate'
  | 'training/webinar'
  | 'other';

export type Segment = 'smb' | 'enterprise' | 'gov' | 'edu' | 'personal';

export type StepId =
  | 'S0'
  | 'S1'
  | 'S2'
  | 'S3'
  | 'S4'
  | 'S5'
  | 'S6'
  | 'S7'
  | 'S8'
  | 'S9'
  | 'S10'
  | 'S11'
  | 'S12';

export type RootCause = 'R1' | 'R2' | 'R3' | 'R4' | 'R5' | 'R6' | 'R7' | 'R8' | 'R9' | 'R10';

export type TicketType =
  | 'ADD_SYNONYM'
  | 'ADD_ALIAS'
  | 'CATALOG_GAP'
  | 'FIX_CARD'
  | 'RANKING_RULE'
  | 'AVAILABILITY_ALERT'
  | 'INTENT_DISAMBIGUATION';

export type EventType =
  | 'search_issued'
  | 'suggest_shown'
  | 'suggest_clicked'
  | 'result_impression'
  | 'result_click'
  | 'chip_click'
  | 'add_to_cart'
  | 'quote_request'
  | 'order'
  | 'cross_sell_click'
  | 'refine'
  | 'abandon'
  | 'feedback'
  | 'catalog_gap_lead'
  | 'admin_action';

export interface ApiError {
  error_code: string;
  message: string;
  trace_id?: string | null;
  details?: Record<string, unknown> | null;
}

// --- Публичный поиск и подсказки (B.4.2) ---

export interface ClientContext {
  persona_id?: string | null;
  segment: Segment;
  page_category_id?: string | null;
  recent_queries?: string[];
  clicked_family_ids?: string[];
  purchased_family_ids?: string[];
}

export interface SearchOptions {
  mode: 'smart' | 'legacy';
  llm_enabled: boolean;
  disable_dense?: boolean;
  limit?: number;
  include_trace?: boolean;
}

export interface SearchRequest {
  query: string;
  session_id: string;
  context: ClientContext;
  options: SearchOptions;
}

export interface Badge {
  type: string;
  label: string;
  basis?: Record<string, unknown> | null;
}

export interface OfferView {
  offer_id: string;
  name_raw: string;
  available: boolean;
  price_state: PriceState;
  price_minor: number | null;
  oldprice_minor: number | null;
  discount_pct: number | null;
  term_months: number | null;
  qty_users: number | null;
  license_type: string | null;
  segment_fit: string | null;
  url: string | null;
}

export interface FeatureContribution {
  feature: string;
  value: number;
  contribution: number;
  source: string;
}

export interface ProductCard {
  family_id: string;
  canonical_name: string;
  vendor: string;
  product_class: ProductClass;
  product_type: string;
  image_url: string | null;
  representative: OfferView;
  n_variants: number;
  variant_axes: Record<string, string[]>;
  price_min_minor: number | null;
  badges: Badge[];
  relevance_label: RelevanceLabel;
  fill_reason: string | null;
  matched_fields: string[];
  feature_contributions: FeatureContribution[];
  score: number;
}

export interface CrossSellItem {
  family_id: string;
  canonical_name: string;
  vendor: string;
  relation_type: string;
  source: string;
  evidence_label: string | null;
  evidence_url: string | null;
  price_min_minor: number | null;
  available: boolean;
}

export interface SearchSection {
  section_id: string;
  title: string;
  intent_class: ProductClass | null;
  empty_reason: string | null;
  items: ProductCard[];
}

export interface NavHit {
  entity_id: string;
  kind: 'CATEGORY' | 'CONTENT' | 'EXTERNAL_PRODUCT';
  title: string;
  url: string | null;
  note: string | null;
}

export interface IntentChip {
  label: string;
  query_rewrite: string;
  intent_hint?: string | null;
}

export interface TraceStep {
  id: StepId;
  title: string;
  status: 'ok' | 'skipped' | 'fallback' | 'error';
  ms: number;
  cpu_ms: number;
  summary: string;
  decision: string | null;
  fallback_used: boolean;
  data: Record<string, unknown>;
}

export interface CostMeter {
  llm_calls: number;
  tokens_in: number;
  tokens_cached: number;
  tokens_out: number;
  cpu_ms_total: number;
  cost_micro_rub: number;
  data_tier: DataTier;
}

export interface SearchResponse {
  contract_version: string;
  trace_id: string;
  query_raw: string;
  query_norm: string;
  applied_query: string | null;
  did_you_mean: string | null;
  mode: 'smart' | 'legacy';
  degradation_level: DegradationLevel;
  config_version: number;
  index_version: string;
  hard_null: boolean;
  soft_null: boolean;
  fill_rung: number;
  out_of_catalog_banner: string | null;
  nav_hits: NavHit[];
  intent_chips: IntentChip[];
  sections: SearchSection[];
  cross_sell: CrossSellItem[];
  related_queries: string[];
  trace: TraceStep[];
  cost_meter: CostMeter;
}

export interface SuggestCompletion {
  text: string;
  kind: 'brand' | 'product' | 'alias' | 'category' | 'seed_query' | 'layout_fix';
  score: number;
}

export interface SuggestProductHit {
  family_id: string;
  canonical_name: string;
  vendor: string;
  image_url: string | null;
  price_min_minor: number | null;
  available: boolean;
  has_promo: boolean;
}

export interface SuggestResponse {
  contract_version: string;
  prefix_raw: string;
  layout_corrected: string | null;
  completions: SuggestCompletion[];
  products: SuggestProductHit[];
  related_queries: string[];
  intent_chips: IntentChip[];
  latency_ms: number;
}

export interface EventIn {
  event_type: EventType;
  session_id: string;
  trace_id?: string | null;
  source?: 'live' | 'simulated' | 'gold_replay';
  payload?: Record<string, unknown>;
}

export interface PersonaItem {
  persona_id: string;
  name?: string;
  title?: string;
  label?: string;
  role?: string;
  segment: Segment;
  description?: string;
  recent_queries?: string[];
  purchased_family_ids?: string[];
  page_category_id?: string | null;
}

export interface DemoScenarioItem {
  id?: string;
  label?: string;
  title?: string;
  query: string;
  category?: string;
  note?: string;
  recommended_mode?: 'smart' | 'legacy';
}

export interface ReadyzResponse {
  index_version: string;
  config_version: number;
  degradation: DegradationLevel;
}

// --- Admin: Analytics & E-com (admin_analytics.py) ---

export interface KpiFunnel {
  search_issued: number;
  clicks: number;
  add_to_cart_or_quote: number;
  orders: number;
  abandons: number;
  catalog_gap_leads: number;
}

export interface KpiSourceStats {
  n_searches_smart: number;
  n_searches_legacy: number;
  hard_null_rate_smart: number;
  soft_null_rate_smart: number;
  hard_null_rate_legacy: number | null;
  abandon_rate: number;
  ctr: number;
  search_to_order_cvr: number;
  funnel: KpiFunnel;
  data_tier: DataTier;
}

export interface OrganizerKpiCard {
  id: string;
  title: string;
  value: number;
  hard_null?: number;
  legacy_value?: number | null;
  before?: number;
  after?: number;
  aov_proxy_rub?: number;
  data_tier: DataTier;
  caption: string;
}

export interface AdminKpiResponse {
  source_filter: 'all' | 'live' | 'simulated';
  summary: KpiSourceStats;
  by_source: {
    live: KpiSourceStats;
    simulated: KpiSourceStats;
  };
  organizer_kpis: OrganizerKpiCard[];
}

export interface ImpactAssumptions {
  q_month: number;
  f_b: number;
  f_n?: number | null;
  cvr_s: number;
  kappa: number;
  aov_rub?: number | null;
  margin: number;
  p_exit: number;
}

export interface ImpactScenarioOut {
  scenario: 'pessimistic' | 'base' | 'optimistic';
  recovered_sessions_month: number;
  delta_orders_month: number;
  delta_revenue_rub_month: number;
  delta_profit_rub_month: number;
  lost_orders_before_month: number;
  lost_orders_after_month: number;
  exits_before_month: number;
  exits_after_month: number;
  data_tier: DataTier;
}

export interface ImpactReport {
  assumptions_used: ImpactAssumptions;
  scenarios: ImpactScenarioOut[];
  data_tier: DataTier;
  note: string;
}

export interface RankingWeightsRequest {
  margin_weight: number;
  promo_weight: number;
  pop_weight: number;
  comment?: string;
}

export interface RankingWeightsResponse {
  config_version: number;
  ranking_weights: Record<string, number>;
  status: string;
}

export interface EvalSummaryResponse {
  status?: string;
  message?: string;
  run_id?: string;
  git_sha?: string;
  config_version?: number;
  index_version?: string;
  checkpoints?: Record<string, Record<string, unknown>>;
  points?: Record<string, Record<string, unknown>>;
  gold10?: Record<string, unknown> | null;
  data_tier?: DataTier;
  [key: string]: unknown;
}

// --- Admin: Agent & Content (admin_agent.py) ---

export interface OverlayRulePayload {
  kind: 'synonym' | 'alias' | 'pin' | 'demote' | 'boost' | 'catalog_patch';
  key: string;
  value: Record<string, unknown>;
}

export interface TicketItem {
  ticket_id: string;
  ticket_type: TicketType;
  root_cause: RootCause;
  target_role: 'content' | 'product' | 'ecom';
  cluster_key: string;
  affected_queries: string[];
  frequency: number;
  estimated_monthly_rub: number;
  estimation_tier: DataTier;
  confidence: number;
  summary: string;
  evidence: string[];
  proposed_patch: OverlayRulePayload | null;
  dry_run_before_hit4: number | null;
  dry_run_after_hit4: number | null;
  status: 'open' | 'applied' | 'rejected' | 'rolled_back';
  applied_config_version: number | null;
  created_at: string;
}

export interface ApplyTicketResponse {
  ticket_id: string;
  gold10_passed: boolean;
  gold10_details: Array<Record<string, unknown>>;
  new_config_version: number;
  replay_results: Array<Record<string, unknown>>;
}

export interface AgentRunResponse {
  status: string;
  llm_used: boolean;
  clusters_found: number;
  created_tickets: number;
  updated_tickets: number;
  tickets: Array<{
    ticket_id: string;
    cluster_key: string;
    root_cause: RootCause;
    ticket_type: TicketType;
    dry_run_before_hit4: number;
    dry_run_after_hit4: number;
  }>;
}

export interface ConfigVersionMeta {
  config_version: number;
  created_at: string;
  author: string;
  ticket_id: string | null;
  action: string;
  comment: string | null;
  overlay_items_count: number;
  ranking_weights: Record<string, number>;
}

export interface RollbackResponse {
  status: string;
  rolled_back_to: number;
  new_config_version: number;
  restored_overlay_items: number;
  ranking_weights: Record<string, number>;
}

export interface DataQualityItem {
  family_id: string;
  offer_id: string;
  canonical_name: string;
  vendor: string;
  data_quality_score: number | null;
  field: string;
  issue_type: string;
  details: string;
}

export interface DataQualityResponse {
  total: number;
  limit?: number;
  offset?: number;
  issue_type_counts: Record<string, number>;
  items: DataQualityItem[];
  data_tier: DataTier;
}

export interface DemandGapsResponse {
  external_products: Array<{
    entity_id: string;
    canonical_name: string;
    aliases: string[];
    url: string | null;
    evidence_snippet: string | null;
    data_tier: DataTier;
  }>;
  gap_tickets: TicketItem[];
  catalog_gap_leads: Array<{
    ts: string;
    session_id: string;
    source: string;
    query?: string;
    segment?: string;
    [key: string]: unknown;
  }>;
  top_null_queries: Array<{
    query_norm: string;
    cnt: number;
    has_hard_null: number;
    has_soft_null: number;
  }>;
  unavailable_top_vendors: Array<{
    vendor: string;
    total_families: number;
    available_families: number;
    unavailable_families: number;
    unavailable_share: number;
    data_tier: DataTier;
  }>;
  data_tier: DataTier;
}

// --- Admin: Ops & Cost (admin_ops.py) ---

export interface AdminOpsResponse {
  index_version: string;
  config_version: number;
  degradation_level: DegradationLevel;
  chaos_flags: {
    force_disable_llm: boolean;
    force_disable_dense: boolean;
    force_disable_overlay: boolean;
  };
  breaker_open: boolean;
  daily_llm_spend_micro_rub: number;
  daily_llm_budget_micro_rub: number;
  daily_llm_calls: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  n_traces: number;
  qu_cache_entries: number;
  qu_cache_hit_rate_est: number;
  data_tier: DataTier;
}

export interface ChaosUpdateRequest {
  force_disable_llm?: boolean;
  force_disable_dense?: boolean;
  force_disable_overlay?: boolean;
  degradation_level?: DegradationLevel;
}

export interface ChaosUpdateResponse {
  status: string;
  force_disable_llm: boolean;
  force_disable_dense: boolean;
  force_disable_overlay: boolean;
  degradation_level: DegradationLevel;
}

export interface CostByPurposeItem {
  purpose: string;
  calls: number;
  ok_calls: number;
  tokens_in: number;
  tokens_cached: number;
  tokens_out: number;
  cost_micro_rub: number;
  avg_latency_ms: number;
  cost_rub: number;
  share_pct: number;
}

export interface AdminCostResponse {
  total_calls: number;
  total_cost_micro_rub: number;
  total_cost_rub: number;
  avg_cost_per_call_micro_rub: number;
  p50_cost_micro_rub: number;
  p95_cost_micro_rub: number;
  avg_cost_per_search_micro_rub: number;
  avg_cost_per_search_rub: number;
  n_search_traces: number;
  by_purpose: CostByPurposeItem[];
  csv_report_path: string;
  csv_exists: boolean;
  data_tier: DataTier;
}

export interface QcReportResponse {
  ingest_sec?: number;
  n_categories: number;
  n_offers: number;
  offers_available_true: number;
  offers_available_share: number;
  offers_priced: number;
  offers_on_request: number;
  offers_valid_promo: number;
  n_families: number;
  families_available_any: number;
  families_without_image: number;
  families_is_ru_registry: number;
  product_class_distribution: Record<string, number>;
  issue_counts: Record<string, number>;
  top_15_vendors_by_families: Array<[string, number]>;
  gold10_coverage?: Array<{
    query: string;
    target_url: string;
    url_found_in_offers: boolean;
    name_tokens_found_in_families: boolean;
  }>;
  index_version?: string;
  n_entities?: number;
  n_graph_edges?: number;
  data_tier?: DataTier;
}