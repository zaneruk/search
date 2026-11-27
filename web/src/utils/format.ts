/**
 * Форматирование денег, дат и процентов с соблюдением инвариантов CONTRACTS.md (B.3, I2).
 * Цена null никогда не отображается как "0 ₽" — только "Цена по запросу".
 */

const RUB_FORMATTER = new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const RUB_DECIMAL_FORMATTER = new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

export function formatPriceMinor(priceMinor: number | null | undefined): string {
  if (priceMinor === null || priceMinor === undefined || priceMinor <= 0) {
    return 'Цена по запросу';
  }
  const rub = Math.round(priceMinor / 100);
  return `${RUB_FORMATTER.format(rub)} ₽`;
}

export function formatRub(rub: number | null | undefined): string {
  if (rub === null || rub === undefined || Number.isNaN(rub)) {
    return '—';
  }
  return `${RUB_FORMATTER.format(Math.round(rub))} ₽`;
}

export function formatMicroRub(microRub: number | null | undefined): string {
  if (microRub === null || microRub === undefined || Number.isNaN(microRub)) {
    return '—';
  }
  const rub = microRub / 1_000_000;
  if (rub > 0 && rub < 0.01) {
    return `${rub.toFixed(4)} ₽`;
  }
  return `${RUB_DECIMAL_FORMATTER.format(rub)} ₽`;
}

export function formatPct(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) {
    return '—';
  }
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) {
    return '—';
  }
  if (ms >= 1000) {
    return `${(ms / 1000).toFixed(2)} с`;
  }
  return `${ms.toFixed(1)} мс`;
}

export function formatUtcTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}