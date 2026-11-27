import React, { useState } from 'react';
import { AlertCircle, CheckCircle2, Layers, Send } from 'lucide-react';
import type { ProductCard, ProductClass, SearchSection } from '../api/types';
import { ResultCard } from './ResultCard';

interface SectionBlockProps {
  section: SearchSection;
  rankOffset: number;
  onCardClick: (card: ProductCard, rank: number) => void;
  onAddToCart: (card: ProductCard, rank: number) => void;
  onRequestQuote: (card: ProductCard, rank: number) => void;
  onFeedback: (card: ProductCard, rating: 1 | -1) => void;
  onRequestCatalogGapSupply: (sectionTitle: string) => void;
}

const CLASS_BADGE: Record<ProductClass, string> = {
  software: 'ПО',
  hardware: 'Оборудование',
  cloud_service: 'Облако (IaaS/SaaS)',
  subscription: 'Подписки',
  certificate: 'Сертификаты / ТП',
  'training/webinar': 'Обучение и вебинары',
  other: 'Прочее',
};

export const SectionBlock: React.FC<SectionBlockProps> = ({
  section,
  rankOffset,
  onCardClick,
  onAddToCart,
  onRequestQuote,
  onFeedback,
  onRequestCatalogGapSupply,
}) => {
  const [gapRequested, setGapRequested] = useState(false);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 pb-2">
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-brand-600" />
          <h2 className="text-base font-bold text-slate-900">{section.title}</h2>
          {section.intent_class && (
            <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
              {CLASS_BADGE[section.intent_class] ?? section.intent_class}
            </span>
          )}
        </div>
        <span className="text-xs text-slate-500">позиций: {section.items.length}</span>
      </div>

      {/* Честный пустой блок по интенту (например, серверное железо при catalog_gap) */}
      {section.empty_reason && section.items.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-start gap-2.5">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
            <div>
              <div className="text-sm font-semibold text-amber-950">
                По этому интенту в текущем фиде позиций нет
              </div>
              <div className="mt-0.5 text-xs text-amber-800">{section.empty_reason}</div>
            </div>
          </div>

          <button
            type="button"
            disabled={gapRequested}
            onClick={() => {
              setGapRequested(true);
              onRequestCatalogGapSupply(section.title);
            }}
            className="inline-flex items-center gap-1.5 rounded-lg bg-amber-700 px-3.5 py-2 text-xs font-semibold text-white hover:bg-amber-800 disabled:bg-emerald-600"
          >
            {gapRequested ? (
              <>
                <CheckCircle2 className="h-3.5 w-3.5" />
                Запрос на поставку зафиксирован
              </>
            ) : (
              <>
                <Send className="h-3.5 w-3.5" />
                Запросить поставку
              </>
            )}
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {section.items.map((card, idx) => (
            <ResultCard
              key={card.family_id}
              card={card}
              rank={rankOffset + idx + 1}
              onCardClick={onCardClick}
              onAddToCart={onAddToCart}
              onRequestQuote={onRequestQuote}
              onFeedback={onFeedback}
            />
          ))}
        </div>
      )}
    </section>
  );
};