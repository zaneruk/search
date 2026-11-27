import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  Columns2,
  Compass,
  ExternalLink,
  FolderTree,
  PackageX,
  Send,
  Sparkles,
  UserCheck,
} from 'lucide-react';
import {
  fetchPersonas,
  fetchScenarios,
  logEvent,
  searchCatalog,
} from '../api/client';
import { streamSearch } from '../api/sse';
import type {
  ClientContext,
  CrossSellItem,
  DemoScenarioItem,
  NavHit,
  PersonaItem,
  ProductCard,
  SearchRequest,
  SearchResponse,
  Segment,
  TraceStep,
} from '../api/types';
import { SearchBox } from '../components/SearchBox';
import { SectionBlock } from '../components/SectionBlock';
import { TraceDrawer } from '../components/TraceDrawer';
import { cn, formatPriceMinor } from '../utils/format';

const FALLBACK_PERSONAS: PersonaItem[] = [
  {
    persona_id: 'neutral_default',
    label: 'Нейтральный корпоративный (SMB)',
    segment: 'smb',
    description: 'Стандартная коммерческая выдача без перекоса истории',
  },
  {
    persona_id: 'sysadmin_gov',
    label: 'Сисадмин госкомпании (Gov)',
    segment: 'gov',
    description: 'Приоритет отечественного ПО из Реестра и сертифицированных СЗИ',
  },
  {
    persona_id: 'buyer_edu',
    label: 'Закупщик ВУЗа (Edu)',
    segment: 'edu',
    description: 'Академические лицензии и образовательные редакции',
  },
  {
    persona_id: 'home_personal',
    label: 'Частный покупатель (Personal)',
    segment: 'personal',
    description: 'Домашние и семейные подписки',
  },
];

const FALLBACK_SCENARIOS: DemoScenarioItem[] = [
  { label: 'сервер (омоним)', query: 'сервер', category: 'pain' },
  { label: 'astra linux', query: 'astra linux', category: 'gold' },
  { label: 'крипто про csp 5.0', query: 'крипто про csp 5.0', category: 'gold' },
  { label: 'кибер бэкап', query: 'кибер бэкап', category: 'gold' },
  { label: 'сканер-вс', query: 'сканер-вс', category: 'gold' },
  { label: 'мтс', query: 'мтс', category: 'gold' },
  { label: '365', query: '365', category: 'gold' },
  { label: 'мдаемон (OOC)', query: 'мдаемон', category: 'complex' },
  { label: 'ghbdtn / rfgthcrbq', query: 'rfgthcrbq антивирус', category: 'complex' },
  {
    label: 'NL: защита от шифровальщиков',
    query: 'чем защитить серверы от шифровальщиков',
    category: 'nl',
  },
];

function getOrCreateSessionId(): string {
  const key = 'ss_demo_session_id';
  try {
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const created = `ss_${Math.random().toString(36).slice(2, 12)}`;
    window.sessionStorage.setItem(key, created);
    return created;
  } catch {
    return `ss_${Math.random().toString(36).slice(2, 12)}`;
  }
}

function getPersonaTitle(p: PersonaItem): string {
  return p.label || p.title || p.name || p.role || `${p.persona_id} (${p.segment})`;
}

const NAV_KIND_LABEL: Record<NavHit['kind'], { text: string; icon: React.ReactNode }> = {
  CATEGORY: { text: 'Категория каталога', icon: <FolderTree className="h-4 w-4 text-sky-600" /> },
  CONTENT: { text: 'Раздел / мероприятия', icon: <Compass className="h-4 w-4 text-indigo-600" /> },
  EXTERNAL_PRODUCT: {
    text: 'Внешний продукт (нет в каталоге)',
    icon: <PackageX className="h-4 w-4 text-amber-600" />,
  },
};

export const StorefrontPage: React.FC = () => {
  const sessionId = useMemo(() => getOrCreateSessionId(), []);

  const [personas, setPersonas] = useState<PersonaItem[]>(FALLBACK_PERSONAS);
  const [scenarios, setScenarios] = useState<DemoScenarioItem[]>(FALLBACK_SCENARIOS);
  const [selectedPersonaId, setSelectedPersonaId] = useState<string>(
    FALLBACK_PERSONAS[0]?.persona_id ?? 'neutral_default',
  );

  const [queryInput, setQueryInput] = useState('');
  const [lastExecutedQuery, setLastExecutedQuery] = useState('');
  const [viewMode, setViewMode] = useState<'smart' | 'legacy' | 'side_by_side'>('smart');
  const [llmEnabled, setLlmEnabled] = useState(true);

  const [recentQueries, setRecentQueries] = useState<string[]>([]);
  const [clickedFamilyIds, setClickedFamilyIds] = useState<string[]>([]);

  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [smartResponse, setSmartResponse] = useState<SearchResponse | null>(null);
  const [legacyResponse, setLegacyResponse] = useState<SearchResponse | null>(null);
  const [traceSteps, setTraceSteps] = useState<TraceStep[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [gapLeadSent, setGapLeadSent] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchPersonas(ctrl.signal)
      .then((list) => {
        if (list.length > 0) {
          setPersonas(list);
          setSelectedPersonaId(list[0]?.persona_id ?? 'neutral_default');
        }
      })
      .catch(() => {
        // остаёмся на fallback-персонах
      });

    fetchScenarios(ctrl.signal)
      .then((list) => {
        if (list.length > 0) setScenarios(list);
      })
      .catch(() => {
        // остаёмся на fallback-сценариях
      });

    return () => ctrl.abort();
  }, []);

  const currentPersona = useMemo(
    () => personas.find((p) => p.persona_id === selectedPersonaId) ?? personas[0],
    [personas, selectedPersonaId],
  );

  const activeSegment: Segment = currentPersona?.segment ?? 'smb';

  const buildContext = useCallback((): ClientContext => {
    return {
      persona_id: currentPersona?.persona_id ?? null,
      segment: activeSegment,
      page_category_id: currentPersona?.page_category_id ?? null,
      recent_queries: [
        ...(currentPersona?.recent_queries ?? []),
        ...recentQueries,
      ].slice(-6),
      clicked_family_ids: clickedFamilyIds.slice(-10),
      purchased_family_ids: currentPersona?.purchased_family_ids ?? [],
    };
  }, [currentPersona, activeSegment, recentQueries, clickedFamilyIds]);

  const executeSearch = useCallback(
    async (rawQuery: string, forceMode?: 'smart' | 'legacy' | 'side_by_side') => {
      const q = rawQuery;
      const modeToRun = forceMode ?? viewMode;

      if (abortRef.current) {
        abortRef.current.abort();
      }
      const controller = new AbortController();
      abortRef.current = controller;

      // Если пользователь переформулировал запрос в той же сессии — пишем refine
      if (lastExecutedQuery && lastExecutedQuery.trim() !== q.trim() && q.trim().length > 0) {
        void logEvent({
          event_type: 'refine',
          session_id: sessionId,
          trace_id: smartResponse?.trace_id ?? legacyResponse?.trace_id ?? null,
          payload: { from_query: lastExecutedQuery, to_query: q },
        }).catch(() => {});
      }

      setLastExecutedQuery(q);
      setLoading(true);
      setErrorMsg(null);
      setGapLeadSent(false);
      setTraceSteps([]);

      const ctx = buildContext();

      try {
        if (modeToRun === 'legacy') {
          setSmartResponse(null);
          const req: SearchRequest = {
            query: q,
            session_id: sessionId,
            context: ctx,
            options: { mode: 'legacy', llm_enabled: false, limit: 20, include_trace: true },
          };
          const res = await searchCatalog(req, controller.signal);
          setLegacyResponse(res);
          setTraceSteps(res.trace ?? []);
        } else if (modeToRun === 'side_by_side') {
          const smartReq: SearchRequest = {
            query: q,
            session_id: sessionId,
            context: ctx,
            options: { mode: 'smart', llm_enabled: llmEnabled, limit: 20, include_trace: true },
          };
          const legacyReq: SearchRequest = {
            query: q,
            session_id: sessionId,
            context: ctx,
            options: { mode: 'legacy', llm_enabled: false, limit: 20, include_trace: false },
          };

          const [smartOutcome, legacyOutcome] = await Promise.allSettled([
            (async () => {
              let finalRes: SearchResponse | null = null;
              await streamSearch(
                smartReq,
                {
                  onSpan: (step) => setTraceSteps((prev) => [...prev, step]),
                  onFinal: (res) => {
                    finalRes = res;
                    setSmartResponse(res);
                    if (res.trace.length > 0) setTraceSteps(res.trace);
                  },
                  onError: (err) => {
                    setErrorMsg('message' in err ? err.message : String(err));
                  },
                },
                controller.signal,
              );
              return finalRes;
            })(),
            searchCatalog(legacyReq, controller.signal),
          ]);

          if (legacyOutcome.status === 'fulfilled') {
            setLegacyResponse(legacyOutcome.value);
          } else {
            setLegacyResponse(null);
          }
          if (smartOutcome.status === 'rejected' && !controller.signal.aborted) {
            setErrorMsg((smartOutcome.reason as Error).message);
          }
        } else {
          setLegacyResponse(null);
          const smartReq: SearchRequest = {
            query: q,
            session_id: sessionId,
            context: ctx,
            options: { mode: 'smart', llm_enabled: llmEnabled, limit: 20, include_trace: true },
          };
          await streamSearch(
            smartReq,
            {
              onSpan: (step) => setTraceSteps((prev) => [...prev, step]),
              onFinal: (res) => {
                setSmartResponse(res);
                if (res.trace.length > 0) setTraceSteps(res.trace);
              },
              onError: (err) => {
                setErrorMsg('message' in err ? err.message : String(err));
              },
            },
            controller.signal,
          );
        }

        if (q.trim()) {
          setRecentQueries((prev) => [...prev.filter((x) => x !== q.trim()), q.trim()].slice(-5));
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setErrorMsg((err as Error).message || 'Ошибка выполнения поиска');
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    },
    [viewMode, lastExecutedQuery, sessionId, smartResponse?.trace_id, legacyResponse?.trace_id, buildContext, llmEnabled],
  );

  // Реакция на смену персоны или режима при уже введённом запросе
  const handlePersonaChange = (nextPersonaId: string) => {
    setSelectedPersonaId(nextPersonaId);
  };

  const activeResponse = viewMode === 'legacy' ? legacyResponse : smartResponse;

  const trackCardEvent = (
    eventType: 'result_click' | 'add_to_cart' | 'quote_request',
    card: ProductCard,
    rank: number,
  ) => {
    setClickedFamilyIds((prev) => [...prev.filter((id) => id !== card.family_id), card.family_id]);
    void logEvent({
      event_type: eventType,
      session_id: sessionId,
      trace_id: activeResponse?.trace_id ?? null,
      payload: {
        family_id: card.family_id,
        offer_id: card.representative.offer_id,
        rank,
        price_minor: card.representative.price_minor,
      },
    }).catch(() => {});
  };

  const handleFeedback = (card: ProductCard, rating: 1 | -1) => {
    void logEvent({
      event_type: 'feedback',
      session_id: sessionId,
      trace_id: activeResponse?.trace_id ?? null,
      payload: {
        family_id: card.family_id,
        query: activeResponse?.query_raw ?? queryInput,
        rating,
        liked: rating > 0,
      },
    }).catch(() => {});
  };

  const handleCrossSellClick = (item: CrossSellItem) => {
    void logEvent({
      event_type: 'cross_sell_click',
      session_id: sessionId,
      trace_id: smartResponse?.trace_id ?? null,
      payload: {
        family_id: item.family_id,
        relation_type: item.relation_type,
        source: item.source,
      },
    }).catch(() => {});
  };

  const handleCatalogGapLead = (note: string) => {
    setGapLeadSent(true);
    void logEvent({
      event_type: 'catalog_gap_lead',
      session_id: sessionId,
      trace_id: activeResponse?.trace_id ?? null,
      payload: {
        query: activeResponse?.query_raw ?? queryInput,
        segment: activeSegment,
        note,
      },
    }).catch(() => {});
  };

  const renderResponseContent = (resp: SearchResponse, compact = false) => {
    let runningRank = 0;

    return (
      <div className="space-y-5">
        {/* Плашка автоисправления / did_you_mean */}
        {(resp.applied_query || resp.did_you_mean) && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-sky-200 bg-sky-50 px-4 py-2.5 text-sm text-sky-900">
            <div>
              {resp.applied_query ? (
                <span>
                  Показаны результаты по исправленному запросу:{' '}
                  <strong className="font-semibold">{resp.applied_query}</strong>{' '}
                  <span className="text-xs text-sky-700">(введено: «{resp.query_raw}»)</span>
                </span>
              ) : (
                <span>
                  Возможно, вы искали:{' '}
                  <button
                    type="button"
                    onClick={() => {
                      if (resp.did_you_mean) {
                        setQueryInput(resp.did_you_mean);
                        void executeSearch(resp.did_you_mean);
                      }
                    }}
                    className="font-semibold underline hover:text-brand-700"
                  >
                    {resp.did_you_mean}
                  </button>
                </span>
              )}
            </div>
          </div>
        )}

        {/* Баннер "В каталоге нет" (EXTERNAL_PRODUCT / OOC) */}
        {resp.out_of_catalog_banner && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4">
            <div className="flex items-start gap-2.5">
              <PackageX className="mt-0.5 h-5 w-5 shrink-0 text-rose-600" />
              <div>
                <div className="text-sm font-bold text-rose-950">{resp.out_of_catalog_banner}</div>
                <div className="mt-0.5 text-xs text-rose-800">
                  Ниже подобраны ближайшие совместимые или российские аналоги из каталога. Вы также
                  можете оставить заявку на закупку.
                </div>
              </div>
            </div>
            <button
              type="button"
              disabled={gapLeadSent}
              onClick={() => handleCatalogGapLead('out_of_catalog_banner')}
              className="inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-rose-700 disabled:bg-emerald-600"
            >
              {gapLeadSent ? (
                <>
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  Заявка отправлена
                </>
              ) : (
                <>
                  <Send className="h-3.5 w-3.5" />
                  Запросить поставку
                </>
              )}
            </button>
          </div>
        )}

        {/* NavHits: категории, вебинары/контент, внешние продукты */}
        {resp.nav_hits.length > 0 && (
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            {resp.nav_hits.map((nh) => {
              const meta = NAV_KIND_LABEL[nh.kind];
              return (
                <div
                  key={nh.entity_id}
                  className="flex items-start justify-between gap-3 rounded-xl border border-indigo-200 bg-indigo-50/50 p-3.5"
                >
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5 shrink-0">{meta.icon}</div>
                    <div>
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-indigo-700">
                        {meta.text}
                      </div>
                      <div className="text-sm font-bold text-slate-900">{nh.title}</div>
                      {nh.note && <div className="mt-0.5 text-xs text-slate-600">{nh.note}</div>}
                    </div>
                  </div>
                  {nh.url && (
                    <a
                      href={nh.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-indigo-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-50"
                    >
                      Перейти
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Уточняющие intent_chips */}
        {resp.intent_chips.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-slate-500">Уточнить направление:</span>
            {resp.intent_chips.map((chip) => (
              <button
                key={`${chip.label}-${chip.query_rewrite}`}
                type="button"
                onClick={() => {
                  setQueryInput(chip.query_rewrite);
                  void logEvent({
                    event_type: 'chip_click',
                    session_id: sessionId,
                    trace_id: resp.trace_id,
                    payload: { label: chip.label, rewrite: chip.query_rewrite },
                  }).catch(() => {});
                  void executeSearch(chip.query_rewrite);
                }}
                className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-700 shadow-sm hover:border-brand-600 hover:text-brand-700"
              >
                {chip.label}
                <ArrowRight className="h-3 w-3 text-slate-400" />
              </button>
            ))}
          </div>
        )}

        {/* Секционная выдача */}
        {resp.sections.map((sec) => {
          const currentOffset = runningRank;
          runningRank += sec.items.length;
          return (
            <SectionBlock
              key={sec.section_id}
              section={sec}
              rankOffset={currentOffset}
              onCardClick={(c, r) => trackCardEvent('result_click', c, r)}
              onAddToCart={(c, r) => trackCardEvent('add_to_cart', c, r)}
              onRequestQuote={(c, r) => trackCardEvent('quote_request', c, r)}
              onFeedback={handleFeedback}
              onRequestCatalogGapSupply={(title) => handleCatalogGapLead(`section:${title}`)}
            />
          );
        })}

        {/* Блок допродаж / аналогов (S11) */}
        {!compact && resp.cross_sell.length > 0 && (
          <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-emerald-600" />
                <h3 className="text-sm font-bold text-slate-900">
                  Рекомендуем вместе и аналоги (Cross-sell / Up-sell)
                </h3>
              </div>
              <span className="text-xs text-slate-400">
                Проверено по графу каталога и данным вендоров
              </span>
            </div>

            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {resp.cross_sell.map((cs) => (
                <div
                  key={`${cs.relation_type}-${cs.family_id}`}
                  className="flex flex-col justify-between rounded-lg border border-slate-200 bg-slate-50/70 p-3"
                >
                  <div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-800">
                        {cs.relation_type === 'upsell_to'
                          ? 'Up-sell'
                          : cs.relation_type === 'analog_of'
                            ? 'Аналог'
                            : 'Вместе с этим'}
                      </span>
                      <span className="font-mono text-[10px] text-slate-400">{cs.source}</span>
                    </div>
                    <div className="mt-1.5 text-xs font-semibold text-slate-500">{cs.vendor}</div>
                    <button
                      type="button"
                      onClick={() => {
                        handleCrossSellClick(cs);
                        setQueryInput(cs.canonical_name);
                        void executeSearch(cs.canonical_name);
                      }}
                      className="mt-0.5 text-left text-sm font-semibold text-slate-900 hover:text-brand-600"
                    >
                      {cs.canonical_name}
                    </button>
                    {cs.evidence_label && (
                      <p className="mt-1.5 text-xs text-slate-600">{cs.evidence_label}</p>
                    )}
                  </div>

                  <div className="mt-3 flex items-center justify-between border-t border-slate-200/80 pt-2 text-xs">
                    <span className="font-bold text-slate-900">
                      {cs.price_min_minor
                        ? `от ${formatPriceMinor(cs.price_min_minor)}`
                        : 'Цена по запросу'}
                    </span>
                    {cs.evidence_url && (
                      <a
                        href={cs.evidence_url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={() => handleCrossSellClick(cs)}
                        className="inline-flex items-center gap-1 font-medium text-sky-700 hover:underline"
                      >
                        Источник вендора
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Связанные запросы */}
        {resp.related_queries.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className="text-xs font-medium text-slate-500">Также ищут:</span>
            {resp.related_queries.map((rq) => (
              <button
                key={rq}
                type="button"
                onClick={() => {
                  setQueryInput(rq);
                  void executeSearch(rq);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-100"
              >
                {rq}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-5">
      {/* Панель управления: Персона, режим Legacy/Smart/Side-by-Side, кнопка Trace */}
      <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-4">
          {/* Выбор персоны */}
          <div className="flex flex-wrap items-center gap-2">
            <UserCheck className="h-4 w-4 text-brand-600" />
            <label htmlFor="persona-select" className="text-xs font-semibold text-slate-600">
              Персона:
            </label>
            <select
              id="persona-select"
              value={selectedPersonaId}
              onChange={(e) => handlePersonaChange(e.target.value)}
              className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-800 focus:border-brand-600 focus:outline-none"
            >
              {personas.map((p) => (
                <option key={p.persona_id} value={p.persona_id}>
                  {getPersonaTitle(p)}
                </option>
              ))}
            </select>
            <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-600">
              segment={activeSegment}
            </span>
          </div>

          {/* Переключатель режима и LLM */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 text-xs font-semibold">
              <button
                type="button"
                onClick={() => setViewMode('smart')}
                className={cn(
                  'rounded-md px-3 py-1.5 transition-colors',
                  viewMode === 'smart'
                    ? 'bg-white text-brand-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900',
                )}
              >
                Smart
              </button>
              <button
                type="button"
                onClick={() => setViewMode('legacy')}
                className={cn(
                  'rounded-md px-3 py-1.5 transition-colors',
                  viewMode === 'legacy'
                    ? 'bg-white text-slate-900 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900',
                )}
              >
                Legacy
              </button>
              <button
                type="button"
                onClick={() => setViewMode('side_by_side')}
                className={cn(
                  'inline-flex items-center gap-1 rounded-md px-3 py-1.5 transition-colors',
                  viewMode === 'side_by_side'
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900',
                )}
              >
                <Columns2 className="h-3.5 w-3.5" />
                Side-by-side
              </button>
            </div>

            <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-700">
              <input
                type="checkbox"
                checked={llmEnabled}
                onChange={(e) => setLlmEnabled(e.target.checked)}
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-600"
              />
              <Bot className="h-3.5 w-3.5 text-indigo-600" />
              LLM QU
            </label>

            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-900 bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-slate-800"
            >
              <Activity className="h-3.5 w-3.5 text-emerald-400" />
              Trace ({traceSteps.length})
            </button>
          </div>
        </div>

        {/* Строка поиска с Typeahead */}
        <SearchBox
          value={queryInput}
          segment={activeSegment}
          loading={loading}
          onChange={setQueryInput}
          onSubmit={(q) => void executeSearch(q)}
          onSuggestShown={(prefix, count) => {
            void logEvent({
              event_type: 'suggest_shown',
              session_id: sessionId,
              payload: { prefix, count, segment: activeSegment },
            }).catch(() => {});
          }}
          onSuggestClicked={(text, kind) => {
            void logEvent({
              event_type: 'suggest_clicked',
              session_id: sessionId,
              payload: { text, kind, segment: activeSegment },
            }).catch(() => {});
          }}
          onChipClicked={(chip) => {
            void logEvent({
              event_type: 'chip_click',
              session_id: sessionId,
              payload: { label: chip.label, rewrite: chip.query_rewrite },
            }).catch(() => {});
          }}
        />

        {/* Сценарные кнопки для быстрого демо */}
        <div className="mt-3.5 flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Сценарии демо:
          </span>
          {scenarios.map((sc) => {
            const label = sc.label || sc.title || sc.query;
            return (
              <button
                key={`${label}-${sc.query}`}
                type="button"
                onClick={() => {
                  setQueryInput(sc.query);
                  void executeSearch(sc.query);
                }}
                title={sc.note || sc.query}
                className={cn(
                  'rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                  lastExecutedQuery === sc.query
                    ? 'border-brand-600 bg-brand-50 text-brand-700'
                    : 'border-slate-200 bg-slate-50 text-slate-700 hover:border-slate-300 hover:bg-slate-100',
                )}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Ошибка */}
      {errorMsg && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Выдача */}
      {viewMode === 'side_by_side' && (smartResponse || legacyResponse) ? (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          <div className="space-y-4 rounded-2xl border border-slate-200 bg-slate-100/70 p-4">
            <div className="flex items-center justify-between border-b border-slate-200 pb-2">
              <span className="text-sm font-bold text-slate-800">Legacy-поиск (как было)</span>
              {legacyResponse && (
                <span className="font-mono text-xs text-slate-500">
                  hard_null={String(legacyResponse.hard_null)} ·{' '}
                  {legacyResponse.sections.reduce((a, s) => a + s.items.length, 0)} поз.
                </span>
              )}
            </div>
            {legacyResponse ? (
              renderResponseContent(legacyResponse, true)
            ) : (
              <div className="py-8 text-center text-sm text-slate-500">
                Legacy-ответ недоступен
              </div>
            )}
          </div>

          <div className="space-y-4 rounded-2xl border border-emerald-200 bg-emerald-50/20 p-4">
            <div className="flex items-center justify-between border-b border-emerald-200 pb-2">
              <span className="text-sm font-bold text-emerald-950">Smart Search (наше ядро)</span>
              {smartResponse && (
                <span className="font-mono text-xs text-emerald-700">
                  {smartResponse.degradation_level} · fill_rung={smartResponse.fill_rung} · cfg v
                  {smartResponse.config_version}
                </span>
              )}
            </div>
            {smartResponse ? (
              renderResponseContent(smartResponse, false)
            ) : (
              <div className="py-8 text-center text-sm text-slate-500">
                Загрузка Smart-выдачи...
              </div>
            )}
          </div>
        </div>
      ) : activeResponse ? (
        <div>{renderResponseContent(activeResponse, false)}</div>
      ) : (
        <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center">
          <div className="mx-auto max-w-lg space-y-2">
            <div className="text-base font-semibold text-slate-800">
              Готов к демонстрации умного B2B-поиска Softline
            </div>
            <p className="text-sm text-slate-500">
              Нажмите на любую кнопку сценария выше или начните вводить запрос в строку поиска —
              вы увидите мгновенный Typeahead с товарами на лету, а кнопка{' '}
              <strong>Trace</strong> покажет прохождение шагов S0–S12 по SSE в реальном времени.
            </p>
          </div>
        </div>
      )}

      <TraceDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        streaming={loading && viewMode !== 'legacy'}
        steps={traceSteps}
        response={activeResponse}
      />
    </div>
  );
};