import React, { useState } from 'react';
import {
  BarChart2,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Info,
  Package,
  ShoppingCart,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react';
import type { ProductCard, RelevanceLabel } from '../api/types';
import { cn, formatPriceMinor } from '../utils/format';
import { BadgeChip } from './BadgeChip';
import { FeatureBars } from './FeatureBars';

interface ResultCardProps {
  card: ProductCard;
  rank: number;
  onCardClick: (card: ProductCard, rank: number) => void;
  onAddToCart: (card: ProductCard, rank: number) => void;
  onRequestQuote: (card: ProductCard, rank: number) => void;
  onFeedback: (card: ProductCard, rating: 1 | -1) => void;
}

const RELEVANCE_META: Record<RelevanceLabel, { label: string; style: string }> = {
  exact: { label: 'Точное совпадение', style: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  close: { label: 'Близкое совпадение', style: 'bg-sky-50 text-sky-700 border-sky-200' },
  similar: { label: 'Похожее решение', style: 'bg-amber-50 text-amber-800 border-amber-200' },
  popular: { label: 'Популярное в категории', style: 'bg-slate-100 text-slate-700 border-slate-200' },
};

const AXIS_LABELS: Record<string, string> = {
  term_months: 'Срок (мес.)',
  qty_users: 'Пользователей',
  license_type: 'Тип лицензии',
  segment: 'Сегмент',
};

export const ResultCard: React.FC<ResultCardProps> = ({
  card,
  rank,
  onCardClick,
  onAddToCart,
  onRequestQuote,
  onFeedback,
}) => {
  const [imgBroken, setImgBroken] = useState(false);
  const [showExplain, setShowExplain] = useState(false);
  const [vote, setVote] = useState<1 | -1 | null>(null);
  const [cartSent, setCartSent] = useState(false);

  const rep = card.representative;
  const relMeta = RELEVANCE_META[card.relevance_label] ?? RELEVANCE_META.close;

  // Инвариант I3: старая цена показывается только при валидной скидке (oldprice > price > 0 и <= 70%)
  const hasValidOldPrice =
    rep.price_minor !== null &&
    rep.price_minor > 0 &&
    rep.oldprice_minor !== null &&
    rep.oldprice_minor > rep.price_minor &&
    (rep.discount_pct === null || rep.discount_pct <= 70);

  const isPriced = rep.price_state === 'priced' && rep.price_minor !== null && rep.price_minor > 0;
  const axisEntries = Object.entries(card.variant_axes).filter(([, vals]) => vals.length > 0);

  const handleVote = (val: 1 | -1) => {
    setVote(val);
    onFeedback(card, val);
  };

  const handlePrimaryAction = () => {
    setCartSent(true);
    if (isPriced && rep.available) {
      onAddToCart(card, rank);
    } else {
      onRequestQuote(card, rank);
    }
  };

  return (
    <article className="flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition-shadow hover:shadow-md">
      <div>
        {/* Верхняя строка: релевантность, класс/тип и скор */}
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-[11px] font-semibold text-slate-400">#{rank}</span>
            <span
              className={cn(
                'rounded border px-2 py-0.5 text-[11px] font-medium',
                relMeta.style,
              )}
            >
              {relMeta.label}
            </span>
            <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
              {card.product_type || card.product_class}
            </span>
          </div>
          <span
            className="font-mono text-[11px] text-slate-400"
            title="Итоговый балл ранжирования"
          >
            score {card.score.toFixed(2)}
          </span>
        </div>

        {/* Инвариант I5: обязательное объяснение для similar / popular */}
        {card.fill_reason && (
          <div className="mb-3 flex items-start gap-1.5 rounded-lg border border-amber-200 bg-amber-50/70 px-2.5 py-1.5 text-xs text-amber-900">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
            <span>{card.fill_reason}</span>
          </div>
        )}

        {/* Основной блок: картинка + имя/вендор */}
        <div className="flex gap-3.5">
          <div className="flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-slate-100 bg-slate-50">
            {card.image_url && !imgBroken ? (
              <img
                src={card.image_url}
                alt={card.canonical_name}
                onError={() => setImgBroken(true)}
                className="h-full w-full object-contain p-1"
                loading="lazy"
              />
            ) : (
              <Package className="h-7 w-7 text-slate-300" />
            )}
          </div>

          <div className="min-w-0 flex-1">
            <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {card.vendor || 'Softline'}
            </div>
            <h3 className="mt-0.5 line-clamp-2 text-sm font-semibold leading-snug text-slate-900">
              {rep.url ? (
                <a
                  href={rep.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => onCardClick(card, rank)}
                  className="hover:text-brand-600"
                >
                  {card.canonical_name}
                </a>
              ) : (
                <button
                  type="button"
                  onClick={() => onCardClick(card, rank)}
                  className="text-left hover:text-brand-600"
                >
                  {card.canonical_name}
                </button>
              )}
            </h3>

            {/* Атрибуты выбранного представителя */}
            <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11px] text-slate-500">
              {rep.license_type && <span>Лицензия: {rep.license_type}</span>}
              {rep.term_months && <span>Срок: {rep.term_months} мес.</span>}
              {rep.qty_users && <span>Мест: {rep.qty_users}</span>}
              {rep.segment_fit && <span>Сегмент: {rep.segment_fit}</span>}
            </div>
          </div>
        </div>

        {/* Бейджи (акции, выгода аналога, реестр РФ, наличие) */}
        {card.badges.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {card.badges.map((b, i) => (
              <BadgeChip key={`${b.type}-${i}`} badge={b} />
            ))}
          </div>
        )}

        {/* Оси вариантов внутри family */}
        {(axisEntries.length > 0 || card.n_variants > 1) && (
          <div className="mt-3 rounded-lg bg-slate-50 p-2.5 text-xs">
            <div className="mb-1 flex items-center justify-between text-[11px] font-medium text-slate-500">
              <span>Варианты в линейке: {card.n_variants}</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {axisEntries.map(([axis, vals]) => (
                <div key={axis} className="flex flex-wrap items-center gap-1">
                  <span className="text-[11px] text-slate-500">
                    {AXIS_LABELS[axis] ?? axis}:
                  </span>
                  {vals.slice(0, 4).map((v) => (
                    <span
                      key={v}
                      className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] font-medium text-slate-700"
                    >
                      {v}
                    </span>
                  ))}
                  {vals.length > 4 && (
                    <span className="text-[11px] text-slate-400">+{vals.length - 4}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Совпавшие поля */}
        {card.matched_fields.length > 0 && (
          <div className="mt-2.5 flex flex-wrap items-center gap-1 text-[11px] text-slate-400">
            <span>Совпадение:</span>
            {card.matched_fields.map((mf) => (
              <span key={mf} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                {mf}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Нижняя часть: цена, CTA, фидбэк и Explain */}
      <div className="mt-4 border-t border-slate-100 pt-3">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-bold text-slate-900">
                {isPriced ? formatPriceMinor(rep.price_minor) : 'Цена по запросу'}
              </span>
              {hasValidOldPrice && (
                <span className="text-xs text-slate-400 line-through">
                  {formatPriceMinor(rep.oldprice_minor)}
                </span>
              )}
            </div>
            {card.price_min_minor && rep.price_minor && card.price_min_minor < rep.price_minor && (
              <div className="text-[11px] text-slate-500">
                в линейке от {formatPriceMinor(card.price_min_minor)}
              </div>
            )}
            <div
              className={cn(
                'mt-0.5 text-[11px] font-medium',
                rep.available ? 'text-emerald-700' : 'text-amber-700',
              )}
            >
              {rep.available ? 'В наличии / электронная поставка' : 'Под заказ (available=false)'}
            </div>
          </div>

          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handlePrimaryAction}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-xs font-semibold transition-colors',
                cartSent
                  ? 'bg-emerald-600 text-white'
                  : isPriced && rep.available
                    ? 'bg-brand-600 text-white hover:bg-brand-700'
                    : 'border border-slate-300 bg-white text-slate-800 hover:bg-slate-50',
              )}
            >
              {cartSent ? (
                <>
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {isPriced && rep.available ? 'В корзине' : 'Запрос КП отправлен'}
                </>
              ) : (
                <>
                  <ShoppingCart className="h-3.5 w-3.5" />
                  {isPriced && rep.available ? 'В корзину' : 'Запросить КП'}
                </>
              )}
            </button>

            {rep.url && (
              <a
                href={rep.url}
                target="_blank"
                rel="noreferrer"
                onClick={() => onCardClick(card, rank)}
                className="rounded-lg border border-slate-200 p-2 text-slate-500 hover:bg-slate-50 hover:text-slate-800"
                title="Открыть на store.softline.ru"
              >
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
          </div>
        </div>

        {/* Строка Explain и оценки 👍/👎 */}
        <div className="mt-2.5 flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setShowExplain((v) => !v)}
            className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-slate-800"
          >
            <BarChart2 className="h-3.5 w-3.5" />
            Почему в топе ({card.feature_contributions.length})
            {showExplain ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => handleVote(1)}
              className={cn(
                'rounded p-1 transition-colors',
                vote === 1
                  ? 'bg-emerald-100 text-emerald-700'
                  : 'text-slate-400 hover:bg-slate-100 hover:text-slate-700',
              )}
              title="Релевантно"
            >
              <ThumbsUp className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => handleVote(-1)}
              className={cn(
                'rounded p-1 transition-colors',
                vote === -1
                  ? 'bg-rose-100 text-rose-700'
                  : 'text-slate-400 hover:bg-slate-100 hover:text-slate-700',
              )}
              title="Нерелевантно (попадёт в диагностику агента)"
            >
              <ThumbsDown className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {showExplain && (
          <div className="mt-2.5 rounded-lg border border-slate-100 bg-slate-50 p-2.5">
            <FeatureBars features={card.feature_contributions} />
          </div>
        )}
      </div>
    </article>
  );
};