import React from 'react';
import type { Badge } from '../api/types';
import { cn } from '../utils/format';

interface BadgeChipProps {
  badge: Badge;
}

export const BadgeChip: React.FC<BadgeChipProps> = ({ badge }) => {
  const styleByType: Record<string, string> = {
    promo: 'bg-rose-600 text-white border-rose-700',
    price_advantage: 'bg-emerald-600 text-white border-emerald-700',
    ru_registry: 'bg-blue-50 text-blue-800 border-blue-200',
    available: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    on_request: 'bg-slate-100 text-slate-700 border-slate-300',
  };

  const tooltip = badge.basis
    ? Object.entries(badge.basis)
        .map(([k, v]) => `${k}: ${String(v)}`)
        .join(' · ')
    : undefined;

  return (
    <span
      title={tooltip}
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-tight',
        styleByType[badge.type] ?? 'bg-slate-100 text-slate-700 border-slate-200',
      )}
    >
      {badge.label}
    </span>
  );
};