import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Coins,
  Cpu,
  Database,
  Flame,
  Gauge,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  ApiClientError,
  fetchAdminCost,
  fetchAdminOps,
  fetchQcReport,
  updateChaosFlags,
} from '../api/client';
import type {
  AdminCostResponse,
  AdminOpsResponse,
  ChaosUpdateRequest,
  DegradationLevel,
  QcReportResponse,
} from '../api/types';
import { TierTag } from '../components/TierTag';
import { cn, formatMicroRub, formatMs, formatPct } from '../utils/format';

interface OpsCostPageProps {
  adminToken: string;
}

const DEG_META: Record<
  DegradationLevel,
  { title: string; desc: string; badgeClass: string }
> = {
  L0: {
    title: 'L0 · Полный пайплайн',
    desc: 'LLM QU + Dense-энкодер + Overlay + все 5 полос (L1–L5)',
    badgeClass: 'bg-emerald-100 text-emerald-800 border-emerald-300',
  },
  L1: {
    title: 'L1 · Без LLM (Rule QU + Dense)',
    desc: 'Детерминированный QU + кэш + нейропоиск L4 (SLA < 400 мс)',
    badgeClass: 'bg-amber-100 text-amber-800 border-amber-300',
  },
  L2: {
    title: 'L2 · Без Dense-энкодера',
    desc: 'Только лексические полосы L1, L2 (BM25F), L3 (char-3gram) и L5',
    badgeClass: 'bg-orange-100 text-orange-800 border-orange-300',
  },
  L3: {
    title: 'L3 · Базовый IndexState',
    desc: 'Overlay и персонализация отключены, чтение неизменяемого индекса',
    badgeClass: 'bg-rose-100 text-rose-800 border-rose-300',
  },
};

export const OpsCostPage: React.FC<OpsCostPageProps> = ({ adminToken }) => {
  const [ops, setOps] = useState<AdminOpsResponse | null>(null);
  const [cost, setCost] = useState<AdminCostResponse | null>(null);
  const [qc, setQc] = useState<QcReportResponse | null>(null);

  const [loading, setLoading] = useState(false);
  const [chaosSaving, setChaosSaving] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadAll = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setErrorMsg(null);

      const [opsRes, costRes, qcRes] = await Promise.allSettled([
        fetchAdminOps(adminToken, signal),
        fetchAdminCost(adminToken, signal),
        fetchQcReport(adminToken, signal),
      ]);

      if (signal?.aborted) {
        return;
      }

      if (opsRes.status === 'fulfilled') {
        setOps(opsRes.value);
      } else if ((opsRes.reason as Error).name !== 'AbortError') {
        const err = opsRes.reason;
        setErrorMsg(
          err instanceof ApiClientError && err.status === 401
            ? 'Неверный или пустой X-Admin-Token. Укажите токен из .env в шапке приложения.'
            : (err as Error).message,
        );
      }

      if (costRes.status === 'fulfilled') {
        setCost(costRes.value);
      }
      if (qcRes.status === 'fulfilled') {
        setQc(qcRes.value);
      }

      setLoading(false);
    },
    [adminToken],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void loadAll(ctrl.signal);
    return () => ctrl.abort();
  }, [loadAll]);

  const handleChaosChange = async (patch: ChaosUpdateRequest) => {
    setChaosSaving(true);
    setErrorMsg(null);
    try {
      await updateChaosFlags(patch, adminToken);
      const freshOps = await fetchAdminOps(adminToken);
      setOps(freshOps);
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setChaosSaving(false);
    }
  };

  const budgetPct =
    ops && ops.daily_llm_budget_micro_rub > 0
      ? Math.min(100, (ops.daily_llm_spend_micro_rub / ops.daily_llm_budget_micro_rub) * 100)
      : 0;

  const currentDeg = ops?.degradation_level ?? 'L0';
  const degMeta = DEG_META[currentDeg];

  return (
    <div className="space-y-6">
      {/* Заголовок */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div>
          <h1 className="text-lg font-bold text-slate-900">
            Ops, Деградация (L0–L3), Cost Ledger и Качество фида
          </h1>
          <p className="text-xs text-slate-500">
            Проверка отказоустойчивости в реальном времени, учёт стоимости LLM по{' '}
            <code className="rounded bg-slate-100 px-1 font-mono">cost_ledger</code> и паспорт
            каталога.
          </p>
        </div>

        <button
          type="button"
          onClick={() => void loadAll()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
          Обновить
        </button>
      </div>

      {errorMsg && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Секция 1: Состояние системы + Chaos Engineering */}
      {ops && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
          {/* Метрики времени работы и кэша */}
          <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-7">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-emerald-600" />
                <h2 className="text-sm font-bold text-slate-900">Состояние поискового ядра</h2>
                <TierTag tier={ops.data_tier} />
              </div>

              <span
                className={cn(
                  'rounded-lg border px-2.5 py-1 font-mono text-xs font-bold',
                  degMeta.badgeClass,
                )}
              >
                {degMeta.title}
              </span>
            </div>

            <p className="text-xs text-slate-500">{degMeta.desc}</p>

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
                <div className="text-[11px] text-slate-500">Latency p50 / p95</div>
                <div className="mt-1 font-mono text-sm font-bold text-slate-900">
                  {formatMs(ops.latency_p50_ms)} / {formatMs(ops.latency_p95_ms)}
                </div>
                <div className="mt-0.5 text-[10px] text-slate-400">трасс: {ops.n_traces}</div>
              </div>

              <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
                <div className="text-[11px] text-slate-500">QU Cache</div>
                <div className="mt-1 font-mono text-sm font-bold text-slate-900">
                  {formatPct(ops.qu_cache_hit_rate_est)}
                </div>
                <div className="mt-0.5 text-[10px] text-slate-400">
                  ключей: {ops.qu_cache_entries}
                </div>
              </div>

              <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
                <div className="text-[11px] text-slate-500">Индекс / Конфиг</div>
                <div
                  className="mt-1 truncate font-mono text-xs font-bold text-slate-900"
                  title={ops.index_version}
                >
                  {ops.index_version}
                </div>
                <div className="mt-0.5 font-mono text-[10px] text-slate-500">
                  config v{ops.config_version}
                </div>
              </div>

              <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
                <div className="text-[11px] text-slate-500">Circuit Breaker</div>
                <div
                  className={cn(
                    'mt-1 font-mono text-sm font-bold',
                    ops.breaker_open ? 'text-rose-700' : 'text-emerald-700',
                  )}
                >
                  {ops.breaker_open ? 'OPEN' : 'CLOSED'}
                </div>
                <div className="mt-0.5 text-[10px] text-slate-400">
                  LLM вызовов сегодня: {ops.daily_llm_calls}
                </div>
              </div>
            </div>

            {/* Дневной бюджет LLM */}
            <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
              <div className="mb-1.5 flex items-center justify-between text-xs">
                <span className="font-semibold text-slate-700">
                  Дневной лимит LLM-расходов (защита бюджета)
                </span>
                <span className="font-mono font-bold text-slate-900">
                  {formatMicroRub(ops.daily_llm_spend_micro_rub)} /{' '}
                  {formatMicroRub(ops.daily_llm_budget_micro_rub)}
                </span>
              </div>
              <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
                <div
                  className={cn(
                    'h-full rounded-full transition-all',
                    budgetPct > 85
                      ? 'bg-rose-600'
                      : budgetPct > 50
                        ? 'bg-amber-500'
                        : 'bg-emerald-600',
                  )}
                  style={{ width: `${Math.max(1, Math.round(budgetPct))}%` }}
                />
              </div>
            </div>
          </div>

          {/* Chaos-переключатели */}
          <div className="flex flex-col justify-between rounded-2xl border border-slate-200 bg-white p-5 shadow-sm lg:col-span-5">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Flame className="h-4 w-4 text-rose-600" />
                  <h2 className="text-sm font-bold text-slate-900">
                    Chaos Engineering (демонстрация L0–L3)
                  </h2>
                </div>
                <button
                  type="button"
                  disabled={chaosSaving || currentDeg === 'L0'}
                  onClick={() => void handleChaosChange({ degradation_level: 'L0' })}
                  className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-[11px] font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  <RotateCcw className="h-3 w-3" />
                  Сбросить в L0
                </button>
              </div>
              <p className="mb-4 text-xs text-slate-500">
                Отключите компонент и сразу проверьте поиск на Экране 1 — система сохранит выдачу
                ≥4 товаров без падения.
              </p>

              <div className="space-y-2.5">
                <label className="flex cursor-pointer items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2.5 hover:bg-slate-100">
                  <div>
                    <div className="text-xs font-bold text-slate-900">
                      Отключить LLM (force_disable_llm → L1)
                    </div>
                    <div className="text-[11px] text-slate-500">
                      Имитация таймаута или исчерпания бюджета Yandex AI Studio
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    disabled={chaosSaving}
                    checked={ops.chaos_flags.force_disable_llm}
                    onChange={(e) =>
                      void handleChaosChange({ force_disable_llm: e.target.checked })
                    }
                    className="h-4 w-4 rounded border-slate-300 text-rose-600 focus:ring-rose-600"
                  />
                </label>

                <label className="flex cursor-pointer items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2.5 hover:bg-slate-100">
                  <div>
                    <div className="text-xs font-bold text-slate-900">
                      Отключить Dense-энкодер (force_disable_dense → L2)
                    </div>
                    <div className="text-[11px] text-slate-500">
                      Работают только L1 Entity, L2 BM25F, L3 char-3gram и L5 Type
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    disabled={chaosSaving}
                    checked={ops.chaos_flags.force_disable_dense}
                    onChange={(e) =>
                      void handleChaosChange({ force_disable_dense: e.target.checked })
                    }
                    className="h-4 w-4 rounded border-slate-300 text-rose-600 focus:ring-rose-600"
                  />
                </label>

                <label className="flex cursor-pointer items-center justify-between rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2.5 hover:bg-slate-100">
                  <div>
                    <div className="text-xs font-bold text-slate-900">
                      Отключить Overlay / БД (force_disable_overlay → L3)
                    </div>
                    <div className="text-[11px] text-slate-500">
                      Чтение только базового иммутабельного индекса
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    disabled={chaosSaving}
                    checked={ops.chaos_flags.force_disable_overlay}
                    onChange={(e) =>
                      void handleChaosChange({ force_disable_overlay: e.target.checked })
                    }
                    className="h-4 w-4 rounded border-slate-300 text-rose-600 focus:ring-rose-600"
                  />
                </label>
              </div>
            </div>

            <div className="mt-3 flex items-center gap-1.5 text-[11px] text-slate-400">
              <ShieldAlert className="h-3.5 w-3.5" />
              Состояние переключателей применяется мгновенно ко всем запросам API.
            </div>
          </div>
        </div>
      )}

      {/* Секция 2: Экономика (Cost Ledger) */}
      {cost && (
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Coins className="h-4 w-4 text-amber-600" />
              <h2 className="text-sm font-bold text-slate-900">
                Счётчик стоимости LLM по шагам (cost_ledger)
              </h2>
              <TierTag tier={cost.data_tier} />
            </div>
            <div className="flex items-center gap-2 font-mono text-xs text-slate-500">
              <span>CSV: {cost.csv_report_path}</span>
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 text-[10px] font-semibold',
                  cost.csv_exists
                    ? 'bg-emerald-100 text-emerald-800'
                    : 'bg-slate-100 text-slate-600',
                )}
              >
                {cost.csv_exists ? 'сформирован' : 'не выгружен'}
              </span>
            </div>
          </div>

          <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Всего вызовов LLM</div>
              <div className="mt-1 font-mono text-lg font-bold text-slate-900">
                {cost.total_calls}
              </div>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Суммарно потрачено</div>
              <div className="mt-1 font-mono text-lg font-bold text-slate-900">
                {cost.total_cost_rub.toFixed(4)} ₽
              </div>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Средняя / LLM-вызов</div>
              <div className="mt-1 font-mono text-sm font-bold text-slate-900">
                {formatMicroRub(cost.avg_cost_per_call_micro_rub)}
              </div>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">p50 / p95 за вызов</div>
              <div className="mt-1 font-mono text-xs font-bold text-slate-900">
                {formatMicroRub(cost.p50_cost_micro_rub)} /{' '}
                {formatMicroRub(cost.p95_cost_micro_rub)}
              </div>
            </div>
            <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-3">
              <div className="text-[11px] text-emerald-800">Аморт. / 1 поиск</div>
              <div className="mt-1 font-mono text-sm font-bold text-emerald-900">
                {formatMicroRub(cost.avg_cost_per_search_micro_rub)}
              </div>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Всего трасс поиска</div>
              <div className="mt-1 font-mono text-lg font-bold text-slate-900">
                {cost.n_search_traces}
              </div>
            </div>
          </div>

          {cost.by_purpose.length > 0 ? (
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
              <div className="h-56 lg:col-span-5">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={cost.by_purpose}
                    margin={{ top: 8, right: 12, left: 0, bottom: 8 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="purpose" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip
                      formatter={(val) => [`${Number(val ?? 0).toFixed(4)} ₽`, 'Стоимость']}
                      contentStyle={{ borderRadius: '8px', fontSize: '12px' }}
                    />
                    <Bar dataKey="cost_rub" fill="#d97706" radius={[5, 5, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <div className="overflow-x-auto lg:col-span-7">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                    <tr>
                      <th className="px-2.5 py-2">Purpose</th>
                      <th className="px-2.5 py-2">Вызовы (ok)</th>
                      <th className="px-2.5 py-2">Токены in/cached/out</th>
                      <th className="px-2.5 py-2">Ср. время</th>
                      <th className="px-2.5 py-2">₽</th>
                      <th className="px-2.5 py-2">Доля</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono">
                    {cost.by_purpose.map((row) => (
                      <tr key={row.purpose}>
                        <td className="px-2.5 py-2 font-semibold text-slate-900">{row.purpose}</td>
                        <td className="px-2.5 py-2">
                          {row.calls} ({row.ok_calls})
                        </td>
                        <td className="px-2.5 py-2 text-[11px] text-slate-600">
                          {row.tokens_in} / {row.tokens_cached} / {row.tokens_out}
                        </td>
                        <td className="px-2.5 py-2">{formatMs(row.avg_latency_ms)}</td>
                        <td className="px-2.5 py-2 font-bold text-slate-900">
                          {row.cost_rub.toFixed(4)}
                        </td>
                        <td className="px-2.5 py-2 text-slate-500">{row.share_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-xs text-slate-400">
              В таблице <code className="font-mono">cost_ledger</code> пока нет записей. Выполните
              поиск в режиме L0 или запустите агента с LLM.
            </div>
          )}
        </div>
      )}

      {/* Секция 3: QC-отчёт качества фида */}
      {qc && (
        <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Database className="h-4 w-4 text-sky-600" />
              <h2 className="text-sm font-bold text-slate-900">
                QC-отчёт качества каталога (qc_report.json)
              </h2>
              <TierTag tier={qc.data_tier ?? 'real'} />
            </div>
            {qc.ingest_sec !== undefined && (
              <span className="inline-flex items-center gap-1 font-mono text-xs text-slate-500">
                <Cpu className="h-3.5 w-3.5" />
                Ingest за {qc.ingest_sec} сек
              </span>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Офферов (offers)</div>
              <div className="mt-1 font-mono text-base font-bold text-slate-900">
                {qc.n_offers.toLocaleString('ru-RU')}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Семейств (families)</div>
              <div className="mt-1 font-mono text-base font-bold text-slate-900">
                {qc.n_families.toLocaleString('ru-RU')}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Категорий</div>
              <div className="mt-1 font-mono text-base font-bold text-slate-900">
                {qc.n_categories.toLocaleString('ru-RU')}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Доступно (available)</div>
              <div className="mt-1 font-mono text-base font-bold text-emerald-700">
                {formatPct(qc.offers_available_share)}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">С ценой / по запросу</div>
              <div className="mt-1 font-mono text-xs font-bold text-slate-900">
                {qc.offers_priced.toLocaleString('ru-RU')} /{' '}
                {qc.offers_on_request.toLocaleString('ru-RU')}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 p-3">
              <div className="text-[11px] text-slate-500">Валидные акции / ЕРРП</div>
              <div className="mt-1 font-mono text-xs font-bold text-slate-900">
                {qc.offers_valid_promo} / {qc.families_is_ru_registry}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
            {/* Распределение product_class */}
            <div className="rounded-xl border border-slate-200 p-3.5 text-xs">
              <div className="mb-2 flex items-center gap-1.5 font-bold text-slate-800">
                <Gauge className="h-3.5 w-3.5 text-sky-600" />
                Классы продуктов
              </div>
              <div className="space-y-1.5">
                {Object.entries(qc.product_class_distribution).map(([cls, count]) => (
                  <div key={cls} className="flex items-center justify-between">
                    <span className="font-mono text-slate-700">{cls}</span>
                    <span className="font-mono font-bold text-slate-900">
                      {count.toLocaleString('ru-RU')}
                    </span>
                  </div>
                ))}
              </div>

              <div className="mt-4 mb-2 font-bold text-slate-800">Выявленные проблемы фида</div>
              <div className="space-y-1">
                {Object.entries(qc.issue_counts).map(([iss, count]) => (
                  <div key={iss} className="flex items-center justify-between text-amber-800">
                    <span className="font-mono">{iss}</span>
                    <span className="font-mono font-bold">{count.toLocaleString('ru-RU')}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Топ вендоров */}
            <div className="rounded-xl border border-slate-200 p-3.5 text-xs">
              <div className="mb-2 font-bold text-slate-800">Топ вендоров по числу семейств</div>
              <div className="max-h-60 space-y-1 overflow-y-auto pr-1">
                {qc.top_15_vendors_by_families.map(([vendor, count]) => (
                  <div key={vendor} className="flex items-center justify-between gap-2">
                    <span className="truncate text-slate-700" title={vendor}>
                      {vendor}
                    </span>
                    <span className="font-mono font-semibold text-slate-900">{count}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* GOLD10 coverage */}
            <div className="rounded-xl border border-slate-200 p-3.5 text-xs">
              <div className="mb-2 font-bold text-slate-800">Покрытие GOLD10 в фиде</div>
              {qc.gold10_coverage && qc.gold10_coverage.length > 0 ? (
                <div className="max-h-60 space-y-1.5 overflow-y-auto pr-1">
                  {qc.gold10_coverage.map((g) => (
                    <div
                      key={g.query}
                      className="flex items-center justify-between gap-2 rounded bg-slate-50 px-2 py-1"
                    >
                      <span className="truncate font-medium text-slate-800" title={g.target_url}>
                        {g.query}
                      </span>
                      <span className="inline-flex shrink-0 items-center gap-1 font-mono text-[10px]">
                        {g.name_tokens_found_in_families ? (
                          <span className="inline-flex items-center gap-0.5 text-emerald-700">
                            <CheckCircle2 className="h-3 w-3" />в каталоге
                          </span>
                        ) : (
                          <span className="text-rose-700">нет</span>
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-slate-400">Нет данных покрытия.</div>
              )}
            </div>
          </div>

          {qc.n_entities !== undefined && (
            <div className="flex flex-wrap items-center gap-4 border-t border-slate-100 pt-2 text-xs text-slate-500">
              <span>
                Сущностей (entities): <strong className="font-mono">{qc.n_entities}</strong>
              </span>
              <span>
                Рёбер графа допродаж: <strong className="font-mono">{qc.n_graph_edges}</strong>
              </span>
              <span>
                Семейств без картинки:{' '}
                <strong className="font-mono">{qc.families_without_image}</strong>
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
};