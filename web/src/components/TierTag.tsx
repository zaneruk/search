import React from 'react';
import type { DataTier } from '../api/types';
import { cn } from '../utils/format';

interface TierTagProps {
  tier: DataTier;
  className?: string;
}

const TIER_META: Record<
  DataTier,
  { label: string; style: string; hint: string }
> = {
  real: {
    label: 'REAL',
    style: 'bg-emerald-50 text-emerald-800 border-emerald-200',
    hint: 'Реальные данные фида / GOLD10 / живых действий пользователя',
  },
  derived: {
    label: 'DERIVED',
    style: 'bg-sky-50 text-sky-800 border-sky-200',
    hint: 'Воспроизводимая метрика / синтетика SYN-A / счётчик cost_ledger',
  },
  judged: {
    label: 'JUDGED',
    style: 'bg-indigo-50 text-indigo-800 border-indigo-200',
    hint: 'Оценка LLM-judge + выборочная ручная проверка',
  },
  simulated: {
    label: 'SIMULATED',
    style: 'bg-amber-50 text-amber-800 border-amber-200',
    hint: 'Симулятор B2B-сессий (демонстрация замкнутого цикла)',
  },
  modeled: {
    label: 'MODELED',
    style: 'bg-purple-50 text-purple-800 border-purple-200',
    hint: 'Сценарный расчёт на допущениях (не измерение реального трафика)',
  },
};

export const TierTag: React.FC<TierTagProps> = ({ tier, className }) => {
  const meta = TIER_META[tier] ?? TIER_META.derived;
  return (
    <span
      title={meta.hint}
      className={cn(
        'inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide',
        meta.style,
        className,
      )}
    >
      {meta.label}
    </span>
  );
};