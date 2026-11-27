import type {
  AdminCostResponse,
  AdminKpiResponse,
  AdminOpsResponse,
  AgentRunResponse,
  ApiError,
  ApplyTicketResponse,
  ChaosUpdateRequest,
  ChaosUpdateResponse,
  ConfigVersionMeta,
  DataQualityResponse,
  DemandGapsResponse,
  DemoScenarioItem,
  EvalSummaryResponse,
  EventIn,
  ImpactAssumptions,
  ImpactReport,
  PersonaItem,
  QcReportResponse,
  RankingWeightsRequest,
  RankingWeightsResponse,
  ReadyzResponse,
  RollbackResponse,
  SearchRequest,
  SearchResponse,
  Segment,
  SuggestResponse,
  TicketItem,
} from './types';

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? '';
const ADMIN_TOKEN_STORAGE_KEY = 'ss_admin_token';

export class ApiClientError extends Error {
  readonly status: number;
  readonly errorCode: string;
  readonly traceId: string | null;
  readonly details: Record<string, unknown> | null;

  constructor(
    status: number,
    message: string,
    errorCode = 'HTTP_ERROR',
    traceId: string | null = null,
    details: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
    this.errorCode = errorCode;
    this.traceId = traceId;
    this.details = details;
  }
}

export function getStoredAdminToken(): string {
  try {
    return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setStoredAdminToken(token: string): void {
  try {
    if (token.trim()) {
      window.localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token.trim());
    } else {
      window.localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
    }
  } catch {
    // ignore storage errors in restricted environments
  }
}

export function getApiBase(): string {
  return API_BASE;
}

async function parseErrorResponse(resp: Response): Promise<ApiClientError> {
  const traceHeader = resp.headers.get('X-Trace-Id');
  try {
    const raw = (await resp.json()) as { detail?: ApiError | string } & Partial<ApiError>;
    const payload = typeof raw.detail === 'object' && raw.detail !== null ? raw.detail : raw;
    const msg =
      (typeof raw.detail === 'string' ? raw.detail : payload.message) ||
      `Ошибка HTTP ${resp.status}`;
    return new ApiClientError(
      resp.status,
      msg,
      payload.error_code ?? `HTTP_${resp.status}`,
      payload.trace_id ?? traceHeader,
      payload.details ?? null,
    );
  } catch {
    return new ApiClientError(resp.status, `Ошибка HTTP ${resp.status}`, `HTTP_${resp.status}`, traceHeader);
  }
}

async function requestJson<T>(
  path: string,
  options: {
    method?: 'GET' | 'POST';
    body?: unknown;
    admin?: boolean;
    adminToken?: string;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };

  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (options.admin) {
    const token = options.adminToken ?? getStoredAdminToken();
    if (token) {
      headers['X-Admin-Token'] = token;
    }
  }

  const resp = await fetch(`${API_BASE}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });

  if (!resp.ok) {
    throw await parseErrorResponse(resp);
  }
  return (await resp.json()) as T;
}

// --- Public API ---

export async function fetchReadyz(signal?: AbortSignal): Promise<ReadyzResponse> {
  return requestJson<ReadyzResponse>('/readyz', { signal });
}

export async function fetchSuggest(
  q: string,
  segment: Segment,
  signal?: AbortSignal,
): Promise<SuggestResponse> {
  const params = new URLSearchParams({ q, segment });
  return requestJson<SuggestResponse>(`/v1/suggest?${params.toString()}`, { signal });
}

export async function searchCatalog(
  payload: SearchRequest,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  return requestJson<SearchResponse>('/v1/search', {
    method: 'POST',
    body: payload,
    signal,
  });
}

export async function logEvent(event: EventIn): Promise<{ event_id: string }> {
  return requestJson<{ event_id: string }>('/v1/events', {
    method: 'POST',
    body: { source: 'live', ...event },
  });
}

export async function fetchPersonas(signal?: AbortSignal): Promise<PersonaItem[]> {
  const res = await requestJson<PersonaItem[] | { personas?: PersonaItem[]; items?: PersonaItem[] }>(
    '/v1/personas',
    { signal },
  );
  if (Array.isArray(res)) return res;
  return res.personas ?? res.items ?? [];
}

export async function fetchScenarios(signal?: AbortSignal): Promise<DemoScenarioItem[]> {
  const res = await requestJson<
    DemoScenarioItem[] | { scenarios?: DemoScenarioItem[]; items?: DemoScenarioItem[] }
  >('/v1/scenarios', { signal });
  if (Array.isArray(res)) return res;
  return res.scenarios ?? res.items ?? [];
}

// --- Admin: E-com & Head ---

export async function fetchAdminKpi(
  source: 'all' | 'live' | 'simulated' = 'all',
  adminToken?: string,
  signal?: AbortSignal,
): Promise<AdminKpiResponse> {
  return requestJson<AdminKpiResponse>(`/v1/admin/kpi?source=${source}`, {
    admin: true,
    adminToken,
    signal,
  });
}

export async function calculateImpactModel(
  assumptions: ImpactAssumptions,
  adminToken?: string,
): Promise<ImpactReport> {
  return requestJson<ImpactReport>('/v1/admin/impact-model', {
    method: 'POST',
    body: assumptions,
    admin: true,
    adminToken,
  });
}

export async function updateRankingWeights(
  payload: RankingWeightsRequest,
  adminToken?: string,
): Promise<RankingWeightsResponse> {
  return requestJson<RankingWeightsResponse>('/v1/admin/ranking-weights', {
    method: 'POST',
    body: payload,
    admin: true,
    adminToken,
  });
}

export async function fetchEvalSummary(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<EvalSummaryResponse> {
  return requestJson<EvalSummaryResponse>('/v1/admin/eval-summary', {
    admin: true,
    adminToken,
    signal,
  });
}

// --- Admin: Agent & Content ---

export async function runDiagnosticAgent(
  options: { use_llm?: boolean; max_clusters?: number } = {},
  adminToken?: string,
): Promise<AgentRunResponse> {
  return requestJson<AgentRunResponse>('/v1/admin/agent/run', {
    method: 'POST',
    body: { use_llm: options.use_llm ?? true, max_clusters: options.max_clusters ?? 15 },
    admin: true,
    adminToken,
  });
}

export async function fetchTickets(
  status?: 'open' | 'applied' | 'rejected' | 'rolled_back',
  adminToken?: string,
  signal?: AbortSignal,
): Promise<TicketItem[]> {
  const qs = status ? `?status=${status}` : '';
  return requestJson<TicketItem[]>(`/v1/admin/tickets${qs}`, {
    admin: true,
    adminToken,
    signal,
  });
}

export async function applyTicket(
  ticketId: string,
  adminToken?: string,
): Promise<ApplyTicketResponse> {
  return requestJson<ApplyTicketResponse>(`/v1/admin/tickets/${encodeURIComponent(ticketId)}/apply`, {
    method: 'POST',
    admin: true,
    adminToken,
  });
}

export async function rejectTicket(
  ticketId: string,
  adminToken?: string,
): Promise<{ ticket_id: string; status: string }> {
  return requestJson<{ ticket_id: string; status: string }>(
    `/v1/admin/tickets/${encodeURIComponent(ticketId)}/reject`,
    {
      method: 'POST',
      admin: true,
      adminToken,
    },
  );
}

export async function rollbackOverlay(
  targetVersion?: number,
  adminToken?: string,
): Promise<RollbackResponse> {
  return requestJson<RollbackResponse>('/v1/admin/overlay/rollback', {
    method: 'POST',
    body: targetVersion !== undefined ? { target_version: targetVersion } : {},
    admin: true,
    adminToken,
  });
}

export async function fetchAuditLog(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<ConfigVersionMeta[]> {
  return requestJson<ConfigVersionMeta[]>('/v1/admin/audit', {
    admin: true,
    adminToken,
    signal,
  });
}

export async function fetchDataQuality(
  params: { issue_type?: string; limit?: number; offset?: number } = {},
  adminToken?: string,
  signal?: AbortSignal,
): Promise<DataQualityResponse> {
  const qs = new URLSearchParams();
  if (params.issue_type) qs.set('issue_type', params.issue_type);
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  return requestJson<DataQualityResponse>(`/v1/admin/data-quality${suffix}`, {
    admin: true,
    adminToken,
    signal,
  });
}

export async function fetchDemandGaps(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<DemandGapsResponse> {
  return requestJson<DemandGapsResponse>('/v1/admin/demand-gaps', {
    admin: true,
    adminToken,
    signal,
  });
}

// --- Admin: Ops & Cost ---

export async function fetchAdminOps(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<AdminOpsResponse> {
  return requestJson<AdminOpsResponse>('/v1/admin/ops', {
    admin: true,
    adminToken,
    signal,
  });
}

export async function updateChaosFlags(
  payload: ChaosUpdateRequest,
  adminToken?: string,
): Promise<ChaosUpdateResponse> {
  return requestJson<ChaosUpdateResponse>('/v1/admin/chaos', {
    method: 'POST',
    body: payload,
    admin: true,
    adminToken,
  });
}

export async function fetchAdminCost(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<AdminCostResponse> {
  return requestJson<AdminCostResponse>('/v1/admin/cost', {
    admin: true,
    adminToken,
    signal,
  });
}

export async function fetchQcReport(
  adminToken?: string,
  signal?: AbortSignal,
): Promise<QcReportResponse> {
  return requestJson<QcReportResponse>('/v1/admin/qc-report', {
    admin: true,
    adminToken,
    signal,
  });
}