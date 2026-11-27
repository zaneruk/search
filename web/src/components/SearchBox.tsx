import React, { useEffect, useRef, useState } from 'react';
import { Loader2, Search, X } from 'lucide-react';
import { fetchSuggest } from '../api/client';
import type {
  IntentChip,
  Segment,
  SuggestProductHit,
  SuggestResponse,
} from '../api/types';
import { Typeahead } from './Typeahead';

interface SearchBoxProps {
  value: string;
  segment: Segment;
  loading: boolean;
  onChange: (nextValue: string) => void;
  onSubmit: (query: string) => void;
  onSuggestShown?: (prefix: string, completionsCount: number) => void;
  onSuggestClicked?: (text: string, kind: string) => void;
  onChipClicked?: (chip: IntentChip) => void;
}

export const SearchBox: React.FC<SearchBoxProps> = ({
  value,
  segment,
  loading,
  onChange,
  onSubmit,
  onSuggestShown,
  onSuggestClicked,
  onChipClicked,
}) => {
  const [suggestData, setSuggestData] = useState<SuggestResponse | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const containerRef = useRef<HTMLDivElement>(null);
  const requestSeqRef = useRef(0);
  const lastLoggedSuggestRef = useRef('');

  useEffect(() => {
    const trimmed = value.trim();
    if (!trimmed) {
      setSuggestData(null);
      setIsOpen(false);
      setActiveIndex(-1);
      return;
    }

    const controller = new AbortController();
    const currentSeq = ++requestSeqRef.current;

    const timer = window.setTimeout(async () => {
      try {
        const res = await fetchSuggest(trimmed, segment, controller.signal);
        if (controller.signal.aborted || currentSeq !== requestSeqRef.current) {
          return;
        }
        setSuggestData(res);
        setActiveIndex(-1);

        const dedupKey = `${res.prefix_raw}:${res.completions.length}:${res.products.length}`;
        if (lastLoggedSuggestRef.current !== dedupKey && (res.completions.length > 0 || res.products.length > 0)) {
          lastLoggedSuggestRef.current = dedupKey;
          onSuggestShown?.(res.prefix_raw, res.completions.length);
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setSuggestData(null);
        }
      }
    }, 80);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [value, segment, onSuggestShown]);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const submitQuery = (q: string) => {
    setIsOpen(false);
    setActiveIndex(-1);
    onSubmit(q);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const completions = suggestData?.completions ?? [];

    if (e.key === 'ArrowDown' && isOpen && completions.length > 0) {
      e.preventDefault();
      setActiveIndex((prev) => (prev + 1) % completions.length);
    } else if (e.key === 'ArrowUp' && isOpen && completions.length > 0) {
      e.preventDefault();
      setActiveIndex((prev) => (prev <= 0 ? completions.length - 1 : prev - 1));
    } else if (e.key === 'Escape') {
      setIsOpen(false);
      setActiveIndex(-1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const chosen = activeIndex >= 0 ? completions[activeIndex] : undefined;
      if (isOpen && chosen) {
        onChange(chosen.text);
        onSuggestClicked?.(chosen.text, chosen.kind);
        submitQuery(chosen.text);
      } else {
        submitQuery(value);
      }
    }
  };

  const handleSelectQuery = (queryText: string, kind = 'completion') => {
    onChange(queryText);
    onSuggestClicked?.(queryText, kind);
    submitQuery(queryText);
  };

  const handleSelectProduct = (product: SuggestProductHit) => {
    onChange(product.canonical_name);
    onSuggestClicked?.(product.canonical_name, `product_hit:${product.family_id}`);
    submitQuery(product.canonical_name);
  };

  const handleSelectChip = (chip: IntentChip) => {
    onChange(chip.query_rewrite);
    onChipClicked?.(chip);
    submitQuery(chip.query_rewrite);
  };

  return (
    <div ref={containerRef} className="relative w-full">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submitQuery(value);
        }}
        className="flex items-center overflow-hidden rounded-xl border-2 border-slate-300 bg-white shadow-sm transition-colors focus-within:border-brand-600"
      >
        <div className="pl-3.5 text-slate-400">
          {loading ? (
            <Loader2 className="h-5 w-5 animate-spin text-brand-600" />
          ) : (
            <Search className="h-5 w-5" />
          )}
        </div>

        <input
          type="text"
          value={value}
          onFocus={() => setIsOpen(true)}
          onChange={(e) => {
            onChange(e.target.value);
            setIsOpen(true);
          }}
          onKeyDown={handleKeyDown}
          placeholder="Введите название ПО, оборудования, задачу или код (например: сервер, крипто про csp 5.0, 365, мдаемон)..."
          className="w-full bg-transparent px-3 py-3 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none sm:text-base"
          aria-label="Поисковый запрос"
        />

        {value.length > 0 && (
          <button
            type="button"
            onClick={() => {
              onChange('');
              setSuggestData(null);
              setIsOpen(false);
            }}
            className="p-2 text-slate-400 hover:text-slate-700"
            title="Очистить"
          >
            <X className="h-4 w-4" />
          </button>
        )}

        <button
          type="submit"
          disabled={loading}
          className="shrink-0 bg-brand-600 px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-brand-700 disabled:opacity-60 sm:px-6"
        >
          Найти
        </button>
      </form>

      {isOpen && suggestData && (
        <Typeahead
          data={suggestData}
          activeIndex={activeIndex}
          onSelectQuery={handleSelectQuery}
          onSelectProduct={handleSelectProduct}
          onSelectChip={handleSelectChip}
        />
      )}
    </div>
  );
};