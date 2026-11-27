# ДИ Уилсона (R5), парный бутстрэп 10k, Макнемар (A.7)
"""Статистика для Eval (CONTRACTS.md A.1 R5, A.7):
- 95% доверительный интервал Уилсона (при n < 30 публикуется описательно без ДИ);
- парный бутстрэп по запросам (10 000 ресэмплов, фиксированный seed);
- критерий Макнемара для бинарных исходов (Hit@k).
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np


def wilson_ci(k: int, n: int, z: float = 1.959964) -> Optional[Tuple[float, float]]:
    if n < 30 or n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def proportion_with_ci(hits: Sequence[bool]) -> Dict[str, object]:
    n = len(hits)
    k = sum(1 for x in hits if x)
    rate = round(k / n, 4) if n > 0 else 0.0
    ci = wilson_ci(k, n)
    return {
        "n": n,
        "k": k,
        "value": rate,
        "ci_95": list(ci) if ci is not None else None,
        "descriptive_only": n < 30,
    }


def paired_bootstrap_delta(
    a_scores: Sequence[float],
    b_scores: Sequence[float],
    *,
    n_resamples: int = 10_000,
    seed: int = 42,
) -> Dict[str, float]:
    """Разница (b - a) и 95% ДИ методом парного бутстрэпа."""
    a = np.asarray(a_scores, dtype=np.float64)
    b = np.asarray(b_scores, dtype=np.float64)
    n = len(a)
    if n == 0 or n != len(b):
        return {"delta": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0}

    diffs = b - a
    observed = float(diffs.mean())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_resamples, n))
    boot_means = diffs[idx].mean(axis=1)
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])
    # Двусторонний бутстрэп p-value относительно 0
    p_val = 2.0 * min(float(np.mean(boot_means <= 0.0)), float(np.mean(boot_means >= 0.0)))
    return {
        "delta": round(observed, 4),
        "ci_low": round(float(ci_low), 4),
        "ci_high": round(float(ci_high), 4),
        "p_value": round(min(1.0, p_val), 4),
    }


def mcnemar_test(a_hits: Sequence[bool], b_hits: Sequence[bool]) -> Dict[str, object]:
    """Критерий Макнемара (с поправкой Эдвардса на непрерывность) для парного Hit@k."""
    if len(a_hits) != len(b_hits) or not a_hits:
        return {"n01": 0, "n10": 0, "chi2": 0.0, "p_value": 1.0}
    n01 = sum(1 for a, b in zip(a_hits, b_hits) if (not a) and b)  # B исправил провал A
    n10 = sum(1 for a, b in zip(a_hits, b_hits) if a and (not b))  # B сломал успех A
    total = n01 + n10
    if total == 0:
        return {"n01": 0, "n10": 0, "chi2": 0.0, "p_value": 1.0}
    chi2 = ((abs(n01 - n10) - 1.0) ** 2) / total if abs(n01 - n10) > 0 else 0.0
    # Для df=1: p = erfc(sqrt(chi2 / 2))
    p_val = math.erfc(math.sqrt(max(0.0, chi2) / 2.0))
    return {"n01": n01, "n10": n10, "chi2": round(chi2, 4), "p_value": round(p_val, 6)}