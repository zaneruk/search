import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Database,
  History,
  PackageSearch,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import {
  ApiClientError,
  applyTicket,
  fetchAuditLog,
  fetchDataQuality,
  fetchDemandGaps,
  fetchTickets,
  rejectTicket,
  rollbackOverlay,
  runDiagnosticAgent,
} from '../api/client';
import type {
  AgentRunResponse,
  ApplyTicketResponse,
  ConfigVersionMeta,
  DataQualityResponse,
  DemandGapsResponse,
  RootCause,
  TicketItem,
} from '../api/types';
import { TierTag } from '../components/TierTag';
import { cn, formatRub, formatUtcTimestamp } from '../utils/format';

interface AgentContentPageProps {
  adminToken: string;
}

type SubTab = 'tickets' | 'audit' | 'quality' | 'gaps';

const ROOT_CAUSE_LABELS: Record<RootCause, string> = {
  R1: 'R1 · Опечатка / раскладка / транслит',
  R2: 'R2 · Нет синонима / сленга / аббревиатуры',
  R3: 'R3 · Нет в каталоге (EXTERNAL_PRODUCT)',
  R4: 'R4 · Нет линейки / редакции / версии',
  R5: 'R5 · Слабая карточка / конфликт полей',
  R6: 'R6 · Проблема ранжирования',
  R7: 'R7 · Неоднозначность интента',
  R8: 'R8 · Потеря спроса (available=false)',
  R9: 'R9 · Не-товарный интент',
  R10: 'R10 · Мусор / nonsense',
};

const TICKET_STATUS_STYLE: Record<TicketItem['status'], string> = {
  open: 'bg-sky-50 text-sky-800 border-sky-200',
  applied: 'bg-emerald-50 text-emerald-800 border-emerald-200',
  rejected: 'bg-slate-100 text-slate-600 border-slate-200',
  rolled_back: 'bg-amber-50 text-amber-800 border-amber-200',
};

export const AgentContentPage: React.FC<AgentContentPageProps> = ({ adminToken }) => {
  const [subTab, setSubTab] = useState<SubTab>('tickets');

  const [tickets, setTickets] = useState<TicketItem[]>([]);
  const [ticketStatusFilter, setTicketStatusFilter] = useState<'all' | TicketItem['status']>('all');
  const [auditLog, setAuditLog] = useState<ConfigVersionMeta[]>([]);
  const [dataQuality, setDataQuality] = useState<DataQualityResponse | null>(null);
  const [dqIssueFilter, setDqIssueFilter] = useState<string>('');
  const [dqOffset, setDqOffset] = useState<number>(0);
  const [demandGaps, setDemandGaps] = useState<DemandGapsResponse | null>(null);

  const [useLlmAgent, setUseLlmAgent] = useState(true);
  const [runningAgent, setRunningAgent] = useState(false);
  const [agentRunResult, setAgentRunResult] = useState<AgentRunResponse | null>(null);

  const [applyingId, setApplyingId] = useState<string | null>(null);
  const [lastApplyResult, setLastApplyResult] = useState<ApplyTicketResponse | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [statusNote, setStatusNote] = useState<string | null>(null);

  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadTicketsAndAudit = useCallback(async () => {
    const ctrl = new AbortController();
    setLoading(true);
    setErrorMsg(null);
    try {
      const [tList, aList] = await Promise.all([
        fetchTickets(ticketStatusFilter === 'all' ? undefined : ticketStatusFilter, adminToken, ctrl.signal),
        fetchAuditLog(adminToken, ctrl.signal),
      ]);
      setTickets(tList);
      setAuditLog(aList);
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
  }, [ticketStatusFilter, adminToken]);

  const loadQuality = useCallback(async () => {
    const ctrl = new AbortController();
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetchDataQuality(
        { issue_type: dqIssueFilter || undefined, limit: 25, offset: dqOffset },
        adminToken,
        ctrl.signal,
      );
      setDataQuality(res);
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setErrorMsg((err as Error).message);
      }
    } finally {
      setLoading(false);
    }
    return () => ctrl.abort();
  }, [dqIssueFilter, dqOffset, adminToken]);

  const loadGaps = useCallback(async () => {
    const ctrl = new AbortController();
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetchDemandGaps(adminToken, ctrl.signal);
      setDemandGaps(res);
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setErrorMsg((err as Error).message);
      }
    } finally {
      setLoading(false);
    }
    return () => ctrl.abort();
  }, [adminToken]);

  useEffect(() => {
    if (subTab === 'tickets' || subTab === 'audit') {
      void loadTicketsAndAudit();
    } else if (subTab === 'quality') {
      void loadQuality();
    } else if (subTab === 'gaps') {
      void loadGaps();
    }
  }, [subTab, loadTicketsAndAudit, loadQuality, loadGaps]);

  const handleRunAgent = async () => {
    setRunningAgent(true);
    setErrorMsg(null);
    setStatusNote(null);
    try {
      const res = await runDiagnosticAgent({ use_llm: useLlmAgent, max_clusters: 15 }, adminToken);
      setAgentRunResult(res);
      await loadTicketsAndAudit();
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setRunningAgent(false);
    }
  };

  const handleApplyTicket = async (ticketId: string) => {
    setApplyingId(ticketId);
    setErrorMsg(null);
    setStatusNote(null);
    try {
      const res = await applyTicket(ticketId, adminToken);
      setLastApplyResult(res);
      await loadTicketsAndAudit();
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setApplyingId(null);
    }
  };

  const handleRejectTicket = async (ticketId: string) => {
    setErrorMsg(null);
    try {
      await rejectTicket(ticketId, adminToken);
      await loadTicketsAndAudit();
    } catch (err) {
      setErrorMsg((err as Error).message);
    }
  };

  const handleRollback = async (targetVersion?: number) => {
    setRollingBack(true);
    setErrorMsg(null);
    setStatusNote(null);
    try {
      const res = await rollbackOverlay(targetVersion, adminToken);
      setStatusNote(
        `Выполнен откат к версии v${res.rolled_back_to} -> активна config v${res.new_config_version} (восстановлено правил overlay: ${res.restored_overlay_items}).`,
      );
      await loadTicketsAndAudit();
    } catch (err) {
      setErrorMsg((err as Error).message);
    } finally {
      setRollingBack(false);
    }
  };

  const currentConfigVersion = auditLog[0]?.config_version ?? 1;

  return (
    <div className="space-y-5">
      {/* Верхняя панель управления агентом и под-вкладками */}
      <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-bold text-slate-900">
              Агент диагностики, Контент и Каталог
            </h1>
            <p className="text-xs text-slate-500">
              Замкнутый цикл улучшений: разбор провалов → dry-run → проверка GOLD10 → hot-swap overlay → откат.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-700">
              <input
                type="checkbox"
                checked={useLlmAgent}
                onChange={(e) => setUseLlmAgent(e.target.checked)}
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-600"
              />
              LLM-разбор (deepseek)
            </label>

            <button
              type="button"
              disabled={runningAgent}
              onClick={() => void handleRunAgent()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
            >
              <Play className="h-3.5 w-3.5" />
              {runningAgent ? 'Разбор трасс...' : 'Запустить разбор агента'}
            </button>
          </div>
        </div>

        {/* Под-вкладки */}
        <div className="mt-4 flex flex-wrap gap-1.5 border-t border-slate-100 pt-3">
          {(
            [
              { id: 'tickets', label: `Тикеты агента (${tickets.length})`, icon: Bot },
              { id: 'audit', label: `Аудит и откат (v${currentConfigVersion})`, icon: History },
              { id: 'quality', label: 'Дефекты карточек (data-quality)', icon: Database },
              { id: 'gaps', label: 'Пробелы спроса (demand-gaps)', icon: PackageSearch },
            ] as const
          ).map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setSubTab(id)}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors',
                subTab === id
                  ? 'bg-slate-900 text-white'
                  : 'bg-slate-100 text-slate-700 hover:bg-slate-200',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {errorMsg && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <AlertTriangle className="h-4 w-4 shrink-0 text-rose-600" />
          <span>{errorMsg}</span>
        </div>
      )}

      {statusNote && (
        <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
          <span>{statusNote}</span>
        </div>
      )}

      {agentRunResult && (
        <div className="rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-xs text-sky-900">
          <strong>Агент завершил разбор:</strong> найдено кластеров{' '}
          <span className="font-mono font-bold">{agentRunResult.clusters_found}</span>, создано тикетов{' '}
          <span className="font-mono font-bold">{agentRunResult.created_tickets}</span>, обновлено{' '}
          <span className="font-mono font-bold">{agentRunResult.updated_tickets}</span> (LLM:{' '}
          {agentRunResult.llm_used ? 'да' : 'нет'}).
        </div>
      )}

      {/* Результат применения тикета: GOLD10-check + мгновенный Replay */}
      {lastApplyResult && (
        <div
          className={cn(
            'rounded-2xl border p-4 shadow-sm',
            lastApplyResult.gold10_passed
              ? 'border-emerald-300 bg-emerald-50/60'
              : 'border-rose-300 bg-rose-50',
          )}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {lastApplyResult.gold10_passed ? (
                <ShieldCheck className="h-5 w-5 text-emerald-600" />
              ) : (
                <XCircle className="h-5 w-5 text-rose-600" />
              )}
              <span className="text-sm font-bold text-slate-900">
                {lastApplyResult.gold10_passed
                  ? `Тикет ${lastApplyResult.ticket_id} применён (config v${lastApplyResult.new_config_version}) — GOLD10-check пройден`
                  : `Применение ${lastApplyResult.ticket_id} заблокировано: обнаружена регрессия на GOLD10`}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setLastApplyResult(null)}
              className="text-xs font-medium text-slate-500 hover:text-slate-800"
            >
              Скрыть
            </button>
          </div>

          {lastApplyResult.replay_results.length > 0 && (
            <div className="mt-3 rounded-xl border border-emerald-200 bg-white p-3">
              <div className="mb-2 text-xs font-bold text-slate-800">
                Мгновенный Replay пострадавших запросов после hot-swap:
              </div>
              <div className="space-y-1.5 text-xs">
                {lastApplyResult.replay_results.map((r, i) => (
                  <div
                    key={i}
                    className="flex flex-wrap items-center justify-between gap-2 rounded bg-slate-50 px-2.5 py-1.5"
                  >
                    <span className="font-medium text-slate-900">«{String(r.query ?? '')}»</span>
                    {r.replayed ? (
                      <span className="font-mono text-[11px] text-emerald-700">
                        top1: {String(r.top1_name ?? '—')} · hard_null={String(r.hard_null)}
                      </span>
                    ) : (
                      <span className="text-[11px] text-slate-500">{String(r.note ?? r.error ?? '')}</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          <details className="mt-2.5 text-xs">
            <summary className="cursor-pointer font-semibold text-slate-700">
              Детали проверки GOLD10 ({lastApplyResult.gold10_details.length} строк)
            </summary>
            <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {lastApplyResult.gold10_details.map((d, idx) => (
                <div
                  key={idx}
                  className="flex items-center justify-between rounded border border-slate-200 bg-white px-2.5 py-1 text-[11px]"
                >
                  <span className="truncate font-medium">{String(d.query ?? '')}</span>
                  <span
                    className={cn(
                      'font-mono font-bold',
                      d.passed ? 'text-emerald-700' : 'text-rose-700',
                    )}
                  >
                    {d.passed ? 'PASS' : 'FAIL'}
                  </span>
                </div>
              ))}
            </div>
          </details>
        </div>
      )}

      {/* Вкладка 1: Очередь тикетов */}
      {subTab === 'tickets' && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 text-xs font-semibold">
              {(['all', 'open', 'applied', 'rejected', 'rolled_back'] as const).map((st) => (
                <button
                  key={st}
                  type="button"
                  onClick={() => setTicketStatusFilter(st)}
                  className={cn(
                    'rounded-md px-2.5 py-1 transition-colors',
                    ticketStatusFilter === st
                      ? 'bg-slate-900 text-white'
                      : 'text-slate-600 hover:text-slate-900',
                  )}
                >
                  {st}
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={() => void loadTicketsAndAudit()}
              disabled={loading}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
              Обновить
            </button>
          </div>

          {tickets.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
              Тикетов по выбранному фильтру пока нет. Нажмите «Запустить разбор агента».
            </div>
          ) : (
            <div className="space-y-3">
              {tickets.map((t) => (
                <div
                  key={t.ticket_id}
                  className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs font-bold text-slate-500">
                          {t.ticket_id}
                        </span>
                        <span
                          className={cn(
                            'rounded border px-2 py-0.5 text-[11px] font-semibold uppercase',
                            TICKET_STATUS_STYLE[t.status],
                          )}
                        >
                          {t.status}
                        </span>
                        <span className="rounded bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-800">
                          {t.ticket_type}
                        </span>
                        <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
                          {ROOT_CAUSE_LABELS[t.root_cause] ?? t.root_cause}
                        </span>
                        <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
                          роль: {t.target_role}
                        </span>
                      </div>

                      <p className="text-sm font-semibold text-slate-900">{t.summary}</p>
                    </div>

                    <div className="text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <span className="text-xs text-slate-500">Потенциал потерь:</span>
                        <span className="font-mono text-sm font-bold text-slate-900">
                          {formatRub(t.estimated_monthly_rub)} / мес
                        </span>
                        <TierTag tier={t.estimation_tier} />
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-slate-500">
                        freq={t.frequency} · conf={t.confidence.toFixed(2)}
                        {t.applied_config_version ? ` · cfg v${t.applied_config_version}` : ''}
                      </div>
                    </div>
                  </div>

                  {/* Пострадавшие запросы и Dry-run */}
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-medium text-slate-500">Запросы:</span>
                    {t.affected_queries.map((q) => (
                      <span
                        key={q}
                        className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-[11px] text-slate-800"
                      >
                        {q}
                      </span>
                    ))}

                    {t.dry_run_before_hit4 !== null && t.dry_run_after_hit4 !== null && (
                      <span className="ml-auto rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1 font-mono text-[11px] font-semibold text-emerald-800">
                        Dry-run Hit@4 proxy: {t.dry_run_before_hit4.toFixed(2)} →{' '}
                        {t.dry_run_after_hit4.toFixed(2)}
                      </span>
                    )}
                  </div>

                  {/* Evidence и Proposed Patch */}
                  <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                    <div className="rounded-xl bg-slate-50 p-3 text-xs">
                      <div className="mb-1 font-semibold text-slate-600">Доказательства (Evidence):</div>
                      <ul className="list-inside list-disc space-y-0.5 text-slate-600">
                        {t.evidence.map((ev, i) => (
                          <li key={i}>{ev}</li>
                        ))}
                      </ul>
                    </div>

                    <div className="rounded-xl bg-slate-900 p-3 text-xs text-slate-100">
                      <div className="mb-1 font-semibold text-slate-400">
                        Кандидатный патч (proposed_patch):
                      </div>
                      {t.proposed_patch ? (
                        <pre className="overflow-x-auto font-mono text-[11px] text-emerald-300">
                          {JSON.stringify(t.proposed_patch, null, 2)}
                        </pre>
                      ) : (
                        <div className="text-slate-400">
                          Патч overlay не требуется (бизнес-алерт / расширение ассортимента).
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Кнопки действий */}
                  {t.status === 'open' && (
                    <div className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 pt-3">
                      <button
                        type="button"
                        onClick={() => void handleRejectTicket(t.ticket_id)}
                        className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                      >
                        Отклонить
                      </button>
                      <button
                        type="button"
                        disabled={applyingId === t.ticket_id}
                        onClick={() => void handleApplyTicket(t.ticket_id)}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
                      >
                        <ShieldCheck className="h-3.5 w-3.5" />
                        {applyingId === t.ticket_id
                          ? 'Проверка GOLD10 и применение...'
                          : 'Одобрить и применить (GOLD10-check)'}
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Вкладка 2: Лента аудита и откат */}
      {subTab === 'audit' && (
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                История версий конфигурации и Overlay (config_versions)
              </h2>
              <p className="text-xs text-slate-500">
                Текущая активная версия: <strong className="font-mono">v{currentConfigVersion}</strong>
              </p>
            </div>
            {currentConfigVersion > 1 && (
              <button
                type="button"
                disabled={rollingBack}
                onClick={() => void handleRollback(currentConfigVersion - 1)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-900 hover:bg-amber-100 disabled:opacity-60"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                Откатить на v{currentConfigVersion - 1}
              </button>
            )}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                <tr>
                  <th className="px-3 py-2">Версия</th>
                  <th className="px-3 py-2">Время (UTC)</th>
                  <th className="px-3 py-2">Действие</th>
                  <th className="px-3 py-2">Автор / Тикет</th>
                  <th className="px-3 py-2">Правил Overlay</th>
                  <th className="px-3 py-2">Комментарий</th>
                  <th className="px-3 py-2 text-right">Откат</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {auditLog.map((row) => (
                  <tr key={row.config_version}>
                    <td className="px-3 py-2.5 font-mono font-bold text-slate-900">
                      v{row.config_version}
                      {row.config_version === currentConfigVersion && (
                        <span className="ml-1.5 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-800">
                          active
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-[11px] text-slate-500">
                      {formatUtcTimestamp(row.created_at)}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-slate-700">{row.action}</td>
                    <td className="px-3 py-2.5 text-slate-700">
                      {row.author}
                      {row.ticket_id ? ` (${row.ticket_id})` : ''}
                    </td>
                    <td className="px-3 py-2.5 font-mono">{row.overlay_items_count}</td>
                    <td className="px-3 py-2.5 text-slate-600">{row.comment ?? '—'}</td>
                    <td className="px-3 py-2.5 text-right">
                      {row.config_version < currentConfigVersion && (
                        <button
                          type="button"
                          disabled={rollingBack}
                          onClick={() => void handleRollback(row.config_version)}
                          className="inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
                        >
                          <RotateCcw className="h-3 w-3" />
                          К v{row.config_version}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Вкладка 3: Качество карточек */}
      {subTab === 'quality' && dataQuality && (
        <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-slate-900">
                Проблемы качества карточек (data_quality_issues)
              </h2>
              <TierTag tier={dataQuality.data_tier} />
            </div>
            <span className="font-mono text-xs text-slate-500">Всего записей: {dataQuality.total}</span>
          </div>

          {/* Фильтры по типу дефекта */}
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => {
                setDqIssueFilter('');
                setDqOffset(0);
              }}
              className={cn(
                'rounded-full border px-3 py-1 text-xs font-medium',
                dqIssueFilter === ''
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : 'border-slate-200 bg-slate-50 text-slate-700',
              )}
            >
              Все типы
            </button>
            {Object.entries(dataQuality.issue_type_counts).map(([issueType, cnt]) => (
              <button
                key={issueType}
                type="button"
                onClick={() => {
                  setDqIssueFilter(issueType);
                  setDqOffset(0);
                }}
                className={cn(
                  'rounded-full border px-3 py-1 text-xs font-medium',
                  dqIssueFilter === issueType
                    ? 'border-brand-600 bg-brand-50 text-brand-700'
                    : 'border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100',
                )}
              >
                {issueType} ({cnt})
              </button>
            ))}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
                <tr>
                  <th className="px-3 py-2">Family / Товар</th>
                  <th className="px-3 py-2">Вендор</th>
                  <th className="px-3 py-2">DQ Score</th>
                  <th className="px-3 py-2">Поле / Тип</th>
                  <th className="px-3 py-2">Детали</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {dataQuality.items.map((item, idx) => (
                  <tr key={`${item.family_id}-${item.issue_type}-${idx}`}>
                    <td className="px-3 py-2">
                      <div className="font-medium text-slate-900">{item.canonical_name || '—'}</div>
                      <div className="font-mono text-[10px] text-slate-400">
                        {item.family_id} / {item.offer_id}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-slate-700">{item.vendor || '—'}</td>
                    <td className="px-3 py-2 font-mono">
                      {item.data_quality_score !== null ? item.data_quality_score.toFixed(2) : '—'}
                    </td>
                    <td className="px-3 py-2">
                      <span className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-amber-800">
                        {item.issue_type}
                      </span>
                      <div className="mt-0.5 font-mono text-[10px] text-slate-400">{item.field}</div>
                    </td>
                    <td className="px-3 py-2 text-slate-600">{item.details}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center justify-between pt-2 text-xs">
            <button
              type="button"
              disabled={dqOffset === 0}
              onClick={() => setDqOffset((o) => Math.max(0, o - 25))}
              className="rounded border border-slate-300 px-3 py-1 font-medium disabled:opacity-40"
            >
              Назад
            </button>
            <span className="font-mono text-slate-500">
              {dqOffset + 1}–{Math.min(dqOffset + 25, dataQuality.total)} из {dataQuality.total}
            </span>
            <button
              type="button"
              disabled={dqOffset + 25 >= dataQuality.total}
              onClick={() => setDqOffset((o) => o + 25)}
              className="rounded border border-slate-300 px-3 py-1 font-medium disabled:opacity-40"
            >
              Вперёд
            </button>
          </div>
        </div>
      )}

      {/* Вкладка 4: Пробелы спроса vs каталога */}
      {subTab === 'gaps' && demandGaps && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          {/* Внешние продукты (R3) */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-bold text-slate-900">
                Внешние продукты вне каталога (EXTERNAL_PRODUCT)
              </h2>
              <TierTag tier="real" />
            </div>
            <div className="space-y-2.5">
              {demandGaps.external_products.map((ep) => (
                <div key={ep.entity_id} className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs">
                  <div className="font-bold text-slate-900">{ep.canonical_name}</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {ep.aliases.map((al) => (
                      <span key={al} className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                        {al}
                      </span>
                    ))}
                  </div>
                  {ep.evidence_snippet && (
                    <p className="mt-1.5 text-slate-600">{ep.evidence_snippet}</p>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Вендоры с высокой долей available=false (R8) */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-bold text-slate-900">
                Топ вендоров с дефицитом наличия (available=false)
              </h2>
              <TierTag tier="real" />
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-200 text-slate-500">
                  <tr>
                    <th className="py-1.5 pr-2">Вендор</th>
                    <th className="py-1.5 pr-2">Семейств</th>
                    <th className="py-1.5 pr-2">Недоступно</th>
                    <th className="py-1.5">Доля</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {demandGaps.unavailable_top_vendors.map((v) => (
                    <tr key={v.vendor}>
                      <td className="py-1.5 pr-2 font-medium text-slate-900">{v.vendor}</td>
                      <td className="py-1.5 pr-2 font-mono">{v.total_families}</td>
                      <td className="py-1.5 pr-2 font-mono text-rose-700">{v.unavailable_families}</td>
                      <td className="py-1.5 font-mono">{(v.unavailable_share * 100).toFixed(0)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Топ null-запросов */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-bold text-slate-900">Частые null-запросы в трассах</h2>
              <TierTag tier={demandGaps.data_tier} />
            </div>
            {demandGaps.top_null_queries.length === 0 ? (
              <div className="text-xs text-slate-400">Null-кластеры отсутствуют.</div>
            ) : (
              <div className="space-y-1.5 text-xs">
                {demandGaps.top_null_queries.map((nq) => (
                  <div
                    key={nq.query_norm}
                    className="flex items-center justify-between rounded bg-slate-50 px-3 py-1.5"
                  >
                    <span className="font-mono font-medium text-slate-800">{nq.query_norm}</span>
                    <span className="font-mono text-slate-500">
                      cnt={nq.cnt} · hard={nq.has_hard_null} · soft={nq.has_soft_null}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Лиды на закупку */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-bold text-slate-900">
                Заявки на поставку (catalog_gap_lead)
              </h2>
              <TierTag tier={demandGaps.data_tier} />
            </div>
            {demandGaps.catalog_gap_leads.length === 0 ? (
              <div className="text-xs text-slate-400">Заявок пока нет.</div>
            ) : (
              <div className="space-y-1.5 text-xs">
                {demandGaps.catalog_gap_leads.slice(0, 12).map((lead, idx) => (
                  <div
                    key={`${lead.session_id}-${idx}`}
                    className="flex items-center justify-between rounded bg-amber-50/70 px-3 py-1.5"
                  >
                    <div>
                      <span className="font-semibold text-slate-900">
                        {String(lead.query ?? '—')}
                      </span>
                      <span className="ml-2 text-[11px] text-slate-500">
                        seg={String(lead.segment ?? '—')}
                      </span>
                    </div>
                    <TierTag tier={lead.source === 'live' ? 'real' : 'simulated'} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};