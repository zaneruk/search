import React, { useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Code2,
  Coins,
  Cpu,
  SkipForward,
  X,
  XCircle,
} from 'lucide-react';
import type { SearchResponse, TraceStep } from '../api/types';
import { cn, formatMicroRub, formatMs } from '../utils/format';
import { TierTag } from './TierTag';

interface TraceDrawerProps {
  open: boolean;
  onClose: () => void;
  streaming: boolean;
  steps: TraceStep[];
  response: SearchResponse | null;
}

const STATUS_ICON: Record<TraceStep['status'], React.ReactNode> = {
  ok: <CheckCircle2 className="h-4 w-4 text-emerald-600" />,
  skipped: <SkipForward className="h-4 w-4 text-slate-400" />,
  fallback: <AlertTriangle className="h-4 w-4 text-amber-600" />,
  error: <XCircle className="h-4 w-4 text-rose-600" />,
};

const DEG_STYLE: Record<string, string> = {
  L0: 'bg-emerald-100 text-emerald-800 border-emerald-200',
  L1: 'bg-amber-100 text-amber-800 border-amber-200',
  L2: 'bg-orange-100 text-orange-800 border-orange-200',
  L3: 'bg-rose-100 text-rose-800 border-rose-200',
};

export const TraceDrawer: React.FC<TraceDrawerProps> = ({
  open,
  onClose,
  streaming,
  steps,
  response,
}) => {
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  const [showRawJson, setShowRawJson] = useState(false);

  if (!open) return null;

  const toggleStep = (id: string) => {
    setExpandedSteps((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const totalMs = steps.reduce((acc, s) => acc + (s.ms || 0), 0);

  return (
    <aside
      aria-label="Трассировка поискового пайплайна S0-S12"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-slate-200 bg-white shadow-2xl"
    >
      {/* Шапка drawer */}
      <div className="flex items-center justify-between border-b border-slate-200 bg-slate-900 px-5 py-3.5 text-white">
        <div className="flex items-center gap-2.5">
          <Activity className="h-5 w-5 text-emerald-400" />
          <div>
            <div className="flex items-center gap-2 text-sm font-bold">
              Трассировка пайплайна (S0–S12)
              {streaming && (
                <span className="animate-pulse rounded bg-emerald-500/20 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
                  SSE live…
                </span>
              )}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-400">
              {response ? `trace_id=${response.trace_id} · cfg v${response.config_version}` : 'Ожидание завершения потока...'}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-white"
        >
          <X className="h-5 w-5" />
        </button>
      </div>

      {/* Сводная полоса: деградация, null-флаги, CostMeter */}
      {response && (
        <div className="space-y-2 border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                'rounded border px-2 py-0.5 font-mono font-semibold',
                DEG_STYLE[response.degradation_level] ?? DEG_STYLE.L0,
              )}
            >
              Режим {response.degradation_level}
            </span>
            <span className="rounded bg-slate-200/70 px-2 py-0.5 font-mono text-slate-700">
              mode={response.mode}
            </span>
            <span className="rounded bg-slate-200/70 px-2 py-0.5 font-mono text-slate-700">
              fill_rung={response.fill_rung}
            </span>
            <span
              className={cn(
                'rounded px-2 py-0.5 font-mono',
                response.hard_null ? 'bg-rose-100 text-rose-700' : 'bg-emerald-50 text-emerald-700',
              )}
            >
              hard_null={String(response.hard_null)}
            </span>
            <span
              className={cn(
                'rounded px-2 py-0.5 font-mono',
                response.soft_null ? 'bg-amber-100 text-amber-800' : 'bg-emerald-50 text-emerald-700',
              )}
            >
              soft_null={String(response.soft_null)}
            </span>
            <span className="ml-auto font-mono text-slate-500">Σ {formatMs(totalMs)}</span>
          </div>

          {/* CostMeter */}
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2">
            <div className="flex items-center gap-1.5 font-medium text-slate-700">
              <Coins className="h-3.5 w-3.5 text-amber-600" />
              <span>Стоимость запроса:</span>
              <span className="font-mono font-bold text-slate-900">
                {formatMicroRub(response.cost_meter.cost_micro_rub)}
              </span>
              <TierTag tier={response.cost_meter.data_tier} />
            </div>
            <div className="flex items-center gap-2 font-mono text-[11px] text-slate-500">
              <span>LLM calls: {response.cost_meter.llm_calls}</span>
              <span>
                tok: {response.cost_meter.tokens_in}
                {response.cost_meter.tokens_cached > 0 ? ` (cached ${response.cost_meter.tokens_cached})` : ''} /{' '}
                {response.cost_meter.tokens_out}
              </span>
              <span className="inline-flex items-center gap-1">
                <Cpu className="h-3 w-3" />
                {formatMs(response.cost_meter.cpu_ms_total)}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Лента шагов S0-S12 */}
      <div className="flex-1 space-y-2 overflow-y-auto px-5 py-4">
        {steps.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-sm text-slate-500">
            Выполните поисковый запрос, чтобы увидеть пошаговый разбор S0–S12 в реальном времени.
          </div>
        ) : (
          steps.map((step, idx) => {
            const isExpanded = Boolean(expandedSteps[step.id]);
            const hasData = step.data && Object.keys(step.data).length > 0;
            return (
              <div
                key={`${step.id}-${idx}`}
                className={cn(
                  'rounded-lg border p-3 transition-colors',
                  step.fallback_used
                    ? 'border-amber-200 bg-amber-50/40'
                    : step.status === 'error'
                      ? 'border-rose-200 bg-rose-50/40'
                      : 'border-slate-200 bg-white',
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5 shrink-0">{STATUS_ICON[step.status]}</div>
                    <div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-[11px] font-bold text-white">
                          {step.id}
                        </span>
                        <span className="text-sm font-semibold text-slate-900">{step.title}</span>
                        {step.decision && (
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
                            {step.decision}
                          </span>
                        )}
                        {step.fallback_used && (
                          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">
                            fallback
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-slate-600">{step.summary}</p>
                    </div>
                  </div>

                  <div className="shrink-0 text-right font-mono text-[11px] text-slate-500">
                    <div className="font-semibold text-slate-700">{formatMs(step.ms)}</div>
                    <div className="text-[10px] text-slate-400">cpu {formatMs(step.cpu_ms)}</div>
                  </div>
                </div>

                {hasData && (
                  <div className="mt-2">
                    <button
                      type="button"
                      onClick={() => toggleStep(step.id)}
                      className="inline-flex items-center gap-1 text-[11px] font-medium text-sky-700 hover:text-sky-900"
                    >
                      {isExpanded ? (
                        <ChevronDown className="h-3.5 w-3.5" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5" />
                      )}
                      Детали шага (JSON)
                    </button>
                    {isExpanded && (
                      <pre className="mt-1.5 max-h-56 overflow-auto rounded-md bg-slate-900 p-2.5 font-mono text-[11px] text-slate-100">
                        {JSON.stringify(step.data, null, 2)}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}

        {/* Полный JSON ответа для жюри */}
        {response && (
          <div className="pt-2">
            <button
              type="button"
              onClick={() => setShowRawJson((v) => !v)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
            >
              <Code2 className="h-3.5 w-3.5" />
              {showRawJson ? 'Скрыть полный SearchResponse JSON' : 'Показать полный SearchResponse JSON'}
            </button>
            {showRawJson && (
              <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] text-emerald-300">
                {JSON.stringify(response, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>
    </aside>
  );
};