import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Calculator,
  CheckCircle2,
  RefreshCw,
  Sliders,
  TrendingUp,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  ApiClientError,
  calculateImpactModel,
  fetchAdminKpi,
  fetchAuditLog,
  fetchEvalSummary,
  updateRankingWeights,
} from '../api/client';
import type {
  AdminKpiResponse,
  EvalSummaryResponse,
  ImpactAssumptions,
  ImpactReport,
} from '../api/types';
import { TierTag } from '../components/TierTag';
import { cn, formatPct, formatRub } from '../utils/format';

interface EcomHeadPageProps {
  adminToken: string;
}

const DEFAULT_ASSUMPTIONS: ImpactAssumptions = {
  q_month: 15000,
  f_b: 0.3,
  f_n: null,
  cvr_s: 0.075,
  kappa: 0.3,
  aov_rub: null,
  margin: 0.2,
  p_exit: 0.4,
};

const SCENARIO_TITLES: Record<'pessimistic' | 'base' | 'optimistic', string> = {
  pessimistic: 'Пессимистичный',
  base: 'Базовый',
  optimistic: 'Оптимистичный',
};

function asNum(v: unknown): number | null {
  if (typeof v === 'number' && !Number.isNaN(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

export const EcomHeadPage: React.FC<EcomHeadPageProps> = ({ adminToken }) => {
  const [sourceFilter, setSourceFilter] = useState<'all' | 'live' | 'simulated'>('all');
  const [kpiData, setKpiData] = useState<AdminKpiResponse | null>(null);
  const [evalData, setEvalData] = useState<EvalSummaryResponse | null>(null);
  const [impactReport, setImpactReport] = useState<ImpactReport | null>(null);

  const [assumptions, setAssumptions] = useState<ImpactAssumptions>(DEFAULT_ASSUMPTIONS);

  const [marginWeight, setMarginWeight] = useState<number>(0.12);
  const [promoWeight, setPromoWeight] = useState<number>(0.1);
  const [popWeight, setPopWeight] = useState<number>(0.2);
  const [weightsComment, setWeightsComment] = useState('Настройка баланса маржи и промо из E-com дашборда');
  const [weightsSavedVer, setWeightsSavedVer] = useState<number | null>(null);

  const [loading, setLoading] = useState(false);
  const [savingWeights, setSavingWeights] = useState(false);
  const [calculatingImpact, setCalculatingImpact] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadDashboard = useCallback(async () => {
    const ctrl = new AbortController();
    setLoading(true);
    setErrorMsg(null);
    try {
      const [kpiRes, evalRes, auditRes] = await Promise.all([
        fetchAdminKpi(sourceFilter, adminToken, ctrl.signal),
        fetchEvalSummary(adminToken, ctrl.signal),
        fetchAuditLog(adminToken, ctrl.signal),
      ]);
      setKpiData(kpiRes);
      setEvalData(evalRes);

      const latestAudit = auditRes[0];
      if (latestAudit?.ranking_weights) {
        const w = latestAudit.ranking_weights;
        if (typeof w.margin_proxy === 'number') setMarginWeight(w.margin_proxy);
        if (typeof w.promo === 'number') setPromoWeight(w.promo);
        if (typeof w.popularity === 'number') setPopWeight(w.popularity);
      }

      const impactRes = await calculateImpactModel(assumptions, adminToken);
      setImpactReport(impactRes);
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        const msg =
          err instanceof ApiClientError && err.status === 401
            ? 'Неверный или пустой X-Admin-Token. Укажите токен из .env в шапке приложения.'
            : (err as Error).message;
        setErrorMsg(msg);
      }
    } finally {
      setLoading(false);
    }
    return () => ctrl.abort();
  }, [sourceFilter, adminToken]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  const handleRecalculateImpact = async (e: React.FormEvent) => {
    e.preventDefault();
    setCalculatingImpact(true);
    setErrorMsg(null);
    try {
      const res = await calculateImpactModel(assumptions, adminToken);
      setImpactReport(res);
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setCalculatingImpact(false);
    }
  };

  const handleSaveWeights = async () => {
    setSavingWeights(true);
    setWeightsSavedVer(null);
    setErrorMsg(null);
    try {
      const res = await updateRankingWeights(
        {
          margin_weight: marginWeight,
          promo_weight: promoWeight,
          pop_weight: popWeight,
          comment: weightsComment,
        },
        adminToken,
      );
      setWeightsSavedVer(res.config_version);
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setSavingWeights(false);
    }
  };

  const funnelChartData = useMemo(() => {
    if (!kpiData) return [];
    const f = kpiData.summary.funnel;
    return [
      { stage: 'Поиски', count: f.search_issued, fill: '#0284c7' },
      { stage: 'Клики', count: f.clicks, fill: '#2563eb' },
      { stage: 'Корзина / КП', count: f.add_to_cart_or_quote, fill: '#4f46e5' },
      { stage: 'Заказы', count: f.orders, fill: '#059669' },
      { stage: 'Уходы (abandon)', count: f.abandons, fill: '#e11d48' },
      { stage: 'Заявки на поставку', count: f.catalog_gap_leads, fill: '#d97706' },
    ];
  }, [kpiData]);

  const evalRows = useMemo(() => {
    if (!evalData) return [];
    const rawMap = evalData.checkpoints ?? evalData.points ?? {};
    return Object.entries(rawMap).map(([key, metrics]) => {
      const m = metrics ?? {};
      const hit1 = asNum(m.hit_at_1 ?? m['Hit@1']);
      const hit4 = asNum(m.hit_at_4 ?? m['Hit@4']);
      const mrr10 = asNum(m.mrr_at_10 ?? m['MRR@10']);
      const ndcg10 = asNum(m.ndcg_at_10 ?? m['nDCG@10']);
      const hardNull = asNum(m.hard_null_rate ?? m.hard_null);
      return {
        point: key,
        hit1Pct: hit1 !== null ? Number((hit1 * 100).toFixed(1)) : null,
        hit4Pct: hit4 !== null ? Number((hit4 * 100).toFixed(1)) : null,
        mrr10,
        ndcg10,
        hardNullPct: hardNull !== null ? Number((hardNull * 100).toFixed(1)) : null,
      };
    });
  }, [evalData]);

  return (
    <div className="space-y-6">
      {/* Шапка и фильтр источника трафика */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            Аналитика E-com и Руководителя
          </h1>
          <p className="text-xs text-slate-500">
            Раздельный учёт ярусов данных: живой трафик (`real`), симуляция (`simulated`), оффлайн-оценка (`derived`) и модель эффекта (`modeled`).
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 text-xs font-semibold">
            {(['all', 'live', 'simulated'] as const).map((src) => (
              <button
                key={src}
                type="button"
                onClick={() => setSourceFilter(src)}
                className={cn(
                  'rounded-md px-3 py-1.5 transition-colors',
                  sourceFilter === src
                    ? 'bg-white text-slate-900 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900',
                )}
              >
                {src === 'all' ? 'Все (All)' : src === 'live' ? 'Живые (Live)' : 'Симулятор (Sim)'}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => void loadDashboard()}
            disabled={loading}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
            Обновить
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* 4 KPI организатора */}
      {kpiData && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          {kpiData.organizer_kpis.map((card) => {
            const isOrderDiff = card.id === 'lost_orders_avoided';
            return (
              <div
                key={card.id}
                className="flex flex-col justify-between rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"
              >
                <div>
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-xs font-semibold text-slate-600">{card.title}</span>
                    <TierTag tier={card.data_tier} />
                  </div>
                  <div className="mt-3 flex items-baseline gap-2">
                    <span className="text-2xl font-bold text-slate-900">
                      {isOrderDiff ? `${card.value.toFixed(1)} зак.` : formatPct(card.value)}
                    </span>
                    {card.legacy_value !== undefined && card.legacy_value !== null && (
                      <span className="text-xs text-slate-500">
                        Legacy: <strong className="text-rose-600">{formatPct(card.legacy_value)}</strong>
                      </span>
                    )}
                  </div>
                  {card.hard_null !== undefined && (
                    <div className="mt-1 text-xs text-slate-500">
                      из них hard_null: <span className="font-mono font-semibold">{formatPct(card.hard_null)}</span>
                    </div>
                  )}
                  {isOrderDiff && card.before !== undefined && card.after !== undefined && (
                    <div className="mt-1 text-xs text-slate-500">
                      Потери: {card.before.toFixed(1)} → <strong className="text-emerald-700">{card.after.toFixed(1)}</strong> / мес
                      {card.aov_proxy_rub ? ` (AOV≈${formatRub(card.aov_proxy_rub)})` : ''}
                    </div>
                  )}
                </div>
                <div className="mt-3 border-t border-slate-100 pt-2 text-[11px] text-slate-400">
                  {card.caption}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Воронка + Разбивка Live vs Simulated */}
      {kpiData && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-7">
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-sky-600" />
                <h2 className="text-sm font-bold text-slate-900">
                  Поисковая воронка ({sourceFilter})
                </h2>
              </div>
              <TierTag tier={kpiData.summary.data_tier} />
            </div>

            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={funnelChartData} margin={{ top: 10, right: 16, left: 0, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="stage" tick={{ fontSize: 11, fill: '#475569' }} />
                  <YAxis tick={{ fontSize: 11, fill: '#475569' }} />
                  <Tooltip
                    formatter={(val) => [String(val ?? 0), 'Событий']}
                    contentStyle={{ borderRadius: '8px', fontSize: '12px' }}
                  />
                  <Bar dataKey="count" radius={[6, 6, 0, 0]}>
                    {funnelChartData.map((entry) => (
                      <Cell key={entry.stage} fill={entry.fill} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Строгое разделение live vs simulated (Правило R2) */}
          <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-5">
            <h2 className="text-sm font-bold text-slate-900">
              Срез по источникам трафика (без смешивания)
            </h2>

            {([
              { title: 'Живой трафик (source=live)', stats: kpiData.by_source.live },
              { title: 'Симулятор (source=simulated)', stats: kpiData.by_source.simulated },
            ] as const).map(({ title, stats }) => (
              <div key={title} className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-bold text-slate-800">{title}</span>
                  <TierTag tier={stats.data_tier} />
                </div>
                <div className="grid grid-cols-3 gap-2 text-xs">
                  <div>
                    <div className="text-[11px] text-slate-500">Smart / Legacy</div>
                    <div className="font-mono font-semibold text-slate-900">
                      {stats.n_searches_smart} / {stats.n_searches_legacy}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-slate-500">Hard-null Smart</div>
                    <div className="font-mono font-semibold text-slate-900">
                      {formatPct(stats.hard_null_rate_smart)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-slate-500">Hard-null Legacy</div>
                    <div className="font-mono font-semibold text-rose-600">
                      {formatPct(stats.hard_null_rate_legacy)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-slate-500">CTR</div>
                    <div className="font-mono font-semibold text-slate-900">
                      {formatPct(stats.ctr)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-slate-500">Abandon</div>
                    <div className="font-mono font-semibold text-amber-700">
                      {formatPct(stats.abandon_rate)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-slate-500">CVR в заказ</div>
                    <div className="font-mono font-semibold text-emerald-700">
                      {formatPct(stats.search_to_order_cvr)}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Сравнение P0..P3 (Eval Summary) + Слайдеры весов */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-7">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                Качество поиска на контрольных точках (P0 → P3)
              </h2>
              <p className="text-xs text-slate-500">
                P0 Legacy · P1 BM25F+Fuzzy · P2 Hybrid (+Dense) · P3 Full Smart (+LLM QU)
              </p>
            </div>
            <TierTag tier={evalData?.data_tier ?? 'derived'} />
          </div>

          {evalRows.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-center text-xs text-slate-500">
              {evalData?.message ||
                'Отчёт eval пока не сформирован (ожидается в eval/reports/*.json). После прогона eval/run_eval.py здесь появятся Hit@1, Hit@4, MRR@10 и nDCG@10.'}
            </div>
          ) : (
            <div className="space-y-4">
              <div className="h-52 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={evalRows} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="point" tick={{ fontSize: 11 }} />
                    <YAxis domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
                    <Tooltip
                      formatter={(v) => [`${String(v ?? 0)}%`, '']}
                      contentStyle={{ borderRadius: '8px', fontSize: '12px' }}
                    />
                    <Bar dataKey="hit4Pct" name="Hit@4 (%)" fill="#059669" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="hit1Pct" name="Hit@1 (%)" fill="#0284c7" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-200 text-slate-500">
                    <tr>
                      <th className="py-1.5 pr-3">Конфигурация</th>
                      <th className="py-1.5 pr-3">Hit@1</th>
                      <th className="py-1.5 pr-3">Hit@4</th>
                      <th className="py-1.5 pr-3">MRR@10</th>
                      <th className="py-1.5 pr-3">nDCG@10</th>
                      <th className="py-1.5">Hard-null</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono">
                    {evalRows.map((r) => (
                      <tr key={r.point}>
                        <td className="py-1.5 pr-3 font-sans font-semibold text-slate-800">{r.point}</td>
                        <td className="py-1.5 pr-3">{r.hit1Pct !== null ? `${r.hit1Pct}%` : '—'}</td>
                        <td className="py-1.5 pr-3 font-bold text-emerald-700">
                          {r.hit4Pct !== null ? `${r.hit4Pct}%` : '—'}
                        </td>
                        <td className="py-1.5 pr-3">{r.mrr10 !== null ? r.mrr10.toFixed(3) : '—'}</td>
                        <td className="py-1.5 pr-3">{r.ndcg10 !== null ? r.ndcg10.toFixed(3) : '—'}</td>
                        <td className="py-1.5">{r.hardNullPct !== null ? `${r.hardNullPct}%` : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        {/* Слайдеры весов маржи / акций / популярности */}
        <div className="flex flex-col justify-between rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-5">
          <div>
            <div className="mb-3 flex items-center gap-2">
              <Sliders className="h-4 w-4 text-brand-600" />
              <h2 className="text-sm font-bold text-slate-900">
                Управление коммерческими весами ранжирования
              </h2>
            </div>
            <p className="mb-4 text-xs text-slate-500">
              Маржа и акции работают как тай-брейкер внутри полосы близкой релевантности δ и не ломают точность выдачи. Применение создаёт новую версию конфига без рестарта.
            </p>

            <div className="space-y-4">
              <div>
                <div className="mb-1 flex justify-between text-xs">
                  <span className="font-medium text-slate-700">Маржинальность (margin_proxy)</span>
                  <span className="font-mono font-bold text-slate-900">{marginWeight.toFixed(2)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1.5}
                  step={0.02}
                  value={marginWeight}
                  onChange={(e) => setMarginWeight(Number(e.target.value))}
                  className="w-full accent-brand-600"
                />
              </div>

              <div>
                <div className="mb-1 flex justify-between text-xs">
                  <span className="font-medium text-slate-700">Акции / скидки (promo)</span>
                  <span className="font-mono font-bold text-slate-900">{promoWeight.toFixed(2)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1.5}
                  step={0.02}
                  value={promoWeight}
                  onChange={(e) => setPromoWeight(Number(e.target.value))}
                  className="w-full accent-brand-600"
                />
              </div>

              <div>
                <div className="mb-1 flex justify-between text-xs">
                  <span className="font-medium text-slate-700">Популярность (popularity)</span>
                  <span className="font-mono font-bold text-slate-900">{popWeight.toFixed(2)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1.5}
                  step={0.02}
                  value={popWeight}
                  onChange={(e) => setPopWeight(Number(e.target.value))}
                  className="w-full accent-brand-600"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-slate-600">
                  Комментарий для аудита
                </label>
                <input
                  type="text"
                  value={weightsComment}
                  onChange={(e) => setWeightsComment(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-800 focus:border-brand-600 focus:outline-none"
                />
              </div>
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-100 pt-3">
            {weightsSavedVer !== null ? (
              <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700">
                <CheckCircle2 className="h-4 w-4" />
                Применено (config v{weightsSavedVer})
              </span>
            ) : (
              <span className="text-xs text-slate-400">Hot-swap без перезапуска</span>
            )}
            <button
              type="button"
              disabled={savingWeights}
              onClick={() => void handleSaveWeights()}
              className="rounded-lg bg-brand-600 px-4 py-2 text-xs font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
            >
              {savingWeights ? 'Применение...' : 'Применить веса'}
            </button>
          </div>
        </div>
      </div>

      {/* Калькулятор бизнес-эффекта (3 сценария A.10) */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Calculator className="h-4 w-4 text-purple-600" />
            <h2 className="text-sm font-bold text-slate-900">
              Сценарный калькулятор бизнес-эффекта (A.10)
            </h2>
            <TierTag tier="modeled" />
          </div>
          <span className="text-xs text-slate-500">
            {impactReport?.note ?? 'Сценарный расчёт на допущениях, не измерение реального трафика'}
          </span>
        </div>

        <form onSubmit={(e) => void handleRecalculateImpact(e)} className="grid grid-cols-2 gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 sm:grid-cols-4 lg:grid-cols-8">
          <label className="text-xs">
            <span className="block font-medium text-slate-600">Q / мес</span>
            <input
              type="number"
              min={1000}
              max={200000}
              step={1000}
              value={assumptions.q_month}
              onChange={(e) => setAssumptions((p) => ({ ...p, q_month: Number(e.target.value) }))}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">f_b (до)</span>
            <input
              type="number"
              min={0.05}
              max={0.8}
              step={0.05}
              value={assumptions.f_b}
              onChange={(e) => setAssumptions((p) => ({ ...p, f_b: Number(e.target.value) }))}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">f_n (после)</span>
            <input
              type="number"
              min={0}
              max={0.5}
              step={0.01}
              placeholder="из eval"
              value={assumptions.f_n ?? ''}
              onChange={(e) =>
                setAssumptions((p) => ({
                  ...p,
                  f_n: e.target.value === '' ? null : Number(e.target.value),
                }))
              }
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">CVR_s</span>
            <input
              type="number"
              min={0.01}
              max={0.3}
              step={0.005}
              value={assumptions.cvr_s}
              onChange={(e) => setAssumptions((p) => ({ ...p, cvr_s: Number(e.target.value) }))}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">κ (kappa)</span>
            <input
              type="number"
              min={0.05}
              max={0.9}
              step={0.05}
              value={assumptions.kappa}
              onChange={(e) => setAssumptions((p) => ({ ...p, kappa: Number(e.target.value) }))}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">AOV (₽)</span>
            <input
              type="number"
              min={1000}
              step={1000}
              placeholder="медиана"
              value={assumptions.aov_rub ?? ''}
              onChange={(e) =>
                setAssumptions((p) => ({
                  ...p,
                  aov_rub: e.target.value === '' ? null : Number(e.target.value),
                }))
              }
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <label className="text-xs">
            <span className="block font-medium text-slate-600">Маржа (m)</span>
            <input
              type="number"
              min={0.05}
              max={0.6}
              step={0.05}
              value={assumptions.margin}
              onChange={(e) => setAssumptions((p) => ({ ...p, margin: Number(e.target.value) }))}
              className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            />
          </label>

          <div className="flex items-end">
            <button
              type="submit"
              disabled={calculatingImpact}
              className="w-full rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-slate-800 disabled:opacity-60"
            >
              {calculatingImpact ? 'Расчёт...' : 'Пересчитать'}
            </button>
          </div>
        </form>

        {impactReport && (
          <div className="mt-4 overflow-x-auto">
            <div className="mb-2 text-xs text-slate-500">
              Применённые базовые значения: <span className="font-mono">f_n={impactReport.assumptions_used.f_n}</span>,{' '}
              <span className="font-mono">AOV={formatRub(impactReport.assumptions_used.aov_rub)}</span>,{' '}
              <span className="font-mono">p_exit={impactReport.assumptions_used.p_exit}</span>
            </div>

            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                <tr>
                  <th className="px-3 py-2">Сценарий</th>
                  <th className="px-3 py-2">Восстановлено сессий / мес</th>
                  <th className="px-3 py-2">Δ Заказов / мес</th>
                  <th className="px-3 py-2">Δ Выручки / мес</th>
                  <th className="px-3 py-2">Δ Валовой прибыли / мес</th>
                  <th className="px-3 py-2">Потери заказов (до → после)</th>
                  <th className="px-3 py-2">Уходы (до → после)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {impactReport.scenarios.map((sc) => (
                  <tr
                    key={sc.scenario}
                    className={cn(sc.scenario === 'base' && 'bg-purple-50/40 font-medium')}
                  >
                    <td className="px-3 py-2.5 font-semibold text-slate-900">
                      <div className="flex items-center gap-2">
                        {SCENARIO_TITLES[sc.scenario]}
                        <TierTag tier={sc.data_tier} />
                      </div>
                    </td>
                    <td className="px-3 py-2.5 font-mono">{sc.recovered_sessions_month.toFixed(0)}</td>
                    <td className="px-3 py-2.5 font-mono text-emerald-700">
                      +{sc.delta_orders_month.toFixed(1)}
                    </td>
                    <td className="px-3 py-2.5 font-mono font-bold text-slate-900">
                      +{formatRub(sc.delta_revenue_rub_month)}
                    </td>
                    <td className="px-3 py-2.5 font-mono font-bold text-emerald-700">
                      +{formatRub(sc.delta_profit_rub_month)}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-slate-600">
                      {sc.lost_orders_before_month.toFixed(1)} → {sc.lost_orders_after_month.toFixed(1)}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-slate-600">
                      {sc.exits_before_month.toFixed(0)} → {sc.exits_after_month.toFixed(0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};