import React from 'react';
import { ArrowUpRight, Sparkles, Tag, Zap } from 'lucide-react';
import type {
  IntentChip,
  SuggestCompletion,
  SuggestProductHit,
  SuggestResponse,
} from '../api/types';
import { cn, formatPriceMinor } from '../utils/format';

interface TypeaheadProps {
  data: SuggestResponse;
  activeIndex: number;
  onSelectQuery: (queryText: string, kind?: string) => void;
  onSelectProduct: (product: SuggestProductHit) => void;
  onSelectChip: (chip: IntentChip) => void;
}

const KIND_LABELS: Record<SuggestCompletion['kind'], { label: string; style: string }> = {
  brand: { label: 'Бренд', style: 'bg-indigo-50 text-indigo-700' },
  product: { label: 'Продукт', style: 'bg-emerald-50 text-emerald-700' },
  alias: { label: 'Алиас', style: 'bg-amber-50 text-amber-700' },
  category: { label: 'Категория', style: 'bg-sky-50 text-sky-700' },
  seed_query: { label: 'Популярное', style: 'bg-slate-100 text-slate-700' },
  layout_fix: { label: 'Раскладка', style: 'bg-rose-50 text-rose-700' },
};

export const Typeahead: React.FC<TypeaheadProps> = ({
  data,
  activeIndex,
  onSelectQuery,
  onSelectProduct,
  onSelectChip,
}) => {
  const hasContent =
    Boolean(data.layout_corrected) ||
    data.completions.length > 0 ||
    data.products.length > 0 ||
    data.intent_chips.length > 0 ||
    data.related_queries.length > 0;

  if (!hasContent) {
    return null;
  }

  return (
    <div
      role="listbox"
      aria-label="Поисковые подсказки"
      className="absolute left-0 right-0 top-full z-40 mt-1.5 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl"
    >
      {/* Шапка: исправление раскладки и серверная задержка */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/80 px-4 py-2 text-xs">
        <div className="flex items-center gap-2">
          {data.layout_corrected ? (
            <button
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => onSelectQuery(data.layout_corrected ?? data.prefix_raw, 'layout_fix')}
              className="inline-flex items-center gap-1 rounded-md bg-rose-50 px-2 py-0.5 font-medium text-rose-700 hover:bg-rose-100"
            >
              <Sparkles className="h-3.5 w-3.5" />
              Раскладка исправлена: <span className="font-semibold">{data.layout_corrected}</span>
            </button>
          ) : (
            <span className="text-slate-500">Быстрые подсказки каталога</span>
          )}
        </div>
        <span className="inline-flex items-center gap-1 font-mono text-[11px] text-slate-500">
          <Zap className="h-3 w-3 text-amber-500" />
          {data.latency_ms.toFixed(1)} мс
        </span>
      </div>

      <div className="grid grid-cols-1 divide-y divide-slate-100 md:grid-cols-12 md:divide-x md:divide-y-0">
        {/* Левая колонка: текстовые подсказки, chips омонимов, связанные запросы */}
        <div className="p-2 md:col-span-5">
          {data.completions.length > 0 && (
            <div className="space-y-0.5">
              {data.completions.map((item, idx) => {
                const meta = KIND_LABELS[item.kind] ?? KIND_LABELS.product;
                const isActive = idx === activeIndex;
                return (
                  <button
                    key={`${item.kind}-${item.text}`}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onSelectQuery(item.text, item.kind)}
                    className={cn(
                      'flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors',
                      isActive ? 'bg-slate-100 text-slate-900' : 'text-slate-700 hover:bg-slate-50',
                    )}
                  >
                    <span className="truncate font-medium">{item.text}</span>
                    <span
                      className={cn(
                        'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold',
                        meta.style,
                      )}
                    >
                      {meta.label}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {data.intent_chips.length > 0 && (
            <div className="mt-2 border-t border-slate-100 px-2 pt-2">
              <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                Уточнить интент
              </div>
              <div className="flex flex-wrap gap-1.5">
                {data.intent_chips.map((chip) => (
                  <button
                    key={`${chip.label}-${chip.query_rewrite}`}
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onSelectChip(chip)}
                    className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-brand-600 hover:bg-brand-50 hover:text-brand-700"
                  >
                    <Tag className="h-3 w-3" />
                    {chip.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {data.related_queries.length > 0 && (
            <div className="mt-2 border-t border-slate-100 px-2 pt-2">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                Связанные запросы
              </div>
              <div className="flex flex-wrap gap-1.5">
                {data.related_queries.slice(0, 3).map((rq) => (
                  <button
                    key={rq}
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onSelectQuery(rq, 'related')}
                    className="rounded-md bg-slate-100 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-200"
                  >
                    {rq}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Правая колонка: Топ-4 товара на лету */}
        <div className="p-2 md:col-span-7">
          <div className="mb-1.5 flex items-center justify-between px-2">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              Товары на лету (Top-{data.products.length})
            </span>
          </div>
          {data.products.length === 0 ? (
            <div className="px-2 py-4 text-xs text-slate-400">
              Подходящих мини-карточек для этого префикса нет — нажмите Enter для полного поиска.
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {data.products.slice(0, 4).map((prod) => (
                <button
                  key={prod.family_id}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => onSelectProduct(prod)}
                  className="group flex items-start gap-2.5 rounded-lg border border-slate-100 bg-white p-2 text-left transition-all hover:border-slate-300 hover:bg-slate-50"
                >
                  <div className="flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-md border border-slate-100 bg-slate-50">
                    {prod.image_url ? (
                      <img
                        src={prod.image_url}
                        alt=""
                        className="h-full w-full object-contain"
                        loading="lazy"
                      />
                    ) : (
                      <span className="text-[10px] font-semibold text-slate-400">SOFT</span>
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-2 text-xs font-medium text-slate-800 group-hover:text-brand-700">
                      {prod.canonical_name}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
                      <span className="font-semibold text-slate-900">
                        {prod.price_min_minor ? `от ${formatPriceMinor(prod.price_min_minor)}` : 'Цена по запросу'}
                      </span>
                      {prod.has_promo && (
                        <span className="rounded bg-rose-100 px-1 py-0.2 text-[10px] font-semibold text-rose-700">
                          Акция
                        </span>
                      )}
                      {!prod.available && (
                        <span className="text-[10px] text-slate-400">под заказ</span>
                      )}
                    </div>
                  </div>
                  <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-slate-300 group-hover:text-brand-600" />
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};