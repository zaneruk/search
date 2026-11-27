import React from 'react';
import type { FeatureContribution } from '../api/types';
import { cn } from '../utils/format';

interface FeatureBarsProps {
  features: FeatureContribution[];
}

const FEATURE_LABELS: Record<string, string> = {
  rel_fused: 'Релевантность (RRF)',
  exact_name_match: 'Точное совпадение',
  availability: 'Наличие',
  popularity: 'Популярность',
  margin_proxy: 'Маржа',
  promo: 'Акция / скидка',
  version_recency: 'Свежесть версии',
  segment_fit: 'Сегмент клиента',
};

export const FeatureBars: React.FC<FeatureBarsProps> = ({ features }) => {
  if (!features || features.length === 0) {
    return (
      <div className="text-xs text-slate-400">
        Детализация вкладов фич недоступна для этого режима.
      </div>
    );
  }

  const maxAbs = Math.max(
    0.001,
    ...features.map((f) => Math.abs(f.contribution)),
  );

  return (
    <div className="space-y-1.5">
      {features.map((f) => {
        const widthPct = Math.min(100, Math.round((Math.abs(f.contribution) / maxAbs) * 100));
        const isNeg = f.contribution < 0;
        const label = FEATURE_LABELS[f.feature] ?? f.feature;
        return (
          <div key={f.feature} className="grid grid-cols-12 items-center gap-2 text-[11px]">
            <div className="col-span-5 truncate text-slate-600" title={`${label} (${f.source})`}>
              {label}{' '}
              <span className="font-mono text-[9px] text-slate-400">[{f.source}]</span>
            </div>
            <div className="col-span-5 h-2 overflow-hidden rounded-full bg-slate-100">
              <div
                className={cn(
                  'h-full rounded-full transition-all',
                  isNeg ? 'bg-rose-500' : 'bg-sky-600',
                )}
                style={{ width: `${widthPct}%` }}
              />
            </div>
            <div
              className={cn(
                'col-span-2 text-right font-mono font-medium',
                isNeg ? 'text-rose-600' : 'text-slate-700',
              )}
            >
              {f.contribution >= 0 ? '+' : ''}
              {f.contribution.toFixed(3)}
            </div>
          </div>
        );
      })}
    </div>
  );
};