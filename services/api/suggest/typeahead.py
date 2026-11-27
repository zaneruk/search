"""Typeahead `/v1/suggest` (T0–T4, бюджет p95 <= 60 мс, без LLM и dense)."""
from __future__ import annotations

import time
from typing import List, Optional

from services.api.app.schemas import IntentChip, Segment, SuggestCompletion, SuggestResponse
from services.api.core.index_state import IndexState
from services.api.core.overlay import OverlaySnapshot
from services.api.nlp.config import basic_norm, get_nlp_config
from services.api.nlp.layout import LayoutFixer
from services.api.nlp.lexicon import get_lexicon
from services.api.nlp.symspell_dict import get_symspell_dict
from services.api.suggest.trie import lookup_trie

_HOMONYM_CHIPS = {
    "сервер": [
        IntentChip(label="Оборудование (серверы)", query_rewrite="сервер оборудование", intent_hint="hardware"),
        IntentChip(label="Облако (аренда IaaS)", query_rewrite="аренда сервера iaas", intent_hint="cloud_service"),
        IntentChip(label="ПО для серверов", query_rewrite="по для серверов", intent_hint="software"),
    ],
    "мтс": [
        IntentChip(label="Вебинары и события", query_rewrite="мтс вебинары", intent_hint="training/webinar"),
        IntentChip(label="МТС Линк / ВКС", query_rewrite="мтс линк", intent_hint="cloud_service"),
    ],
    "365": [
        IntentChip(label="Microsoft 365 для семьи", query_rewrite="microsoft 365 для семьи", intent_hint="subscription"),
        IntentChip(label="Microsoft 365 для бизнеса", query_rewrite="microsoft 365 business", intent_hint="subscription"),
    ],
}


def build_suggest(
    prefix_raw: str,
    *,
    segment: Segment = Segment.smb,
    index_state: IndexState,
    overlay: Optional[OverlaySnapshot] = None,
) -> SuggestResponse:
    t0 = time.perf_counter()
    cfg = get_nlp_config()
    q_norm = basic_norm(prefix_raw)
    lex = get_lexicon(cfg, index_state, overlay)

    layout_corrected: Optional[str] = None
    lookup_key = q_norm

    # T1: Проверка раскладки клавиатуры
    if q_norm and len(q_norm) >= 3:
        fixer = LayoutFixer(cfg, is_known=lex.is_known_term)
        lres = fixer.fix_tokens(q_norm.split())
        if lres.changed:
            layout_corrected = lres.text
            lookup_key = lres.text

    trie_res = lookup_trie(
        lookup_key,
        index_state,
        max_completions=8,
        max_products=4,
    )
    completions = list(trie_res.completions)
    products = list(trie_res.products)

    # Если пусто и длина >= 3 — быстрый fuzzy через SymSpell на последнем слове
    if not completions and len(lookup_key) >= 3:
        sym = get_symspell_dict(cfg, lex, index_state, overlay)
        parts = lookup_key.split()
        sugg = sym.lookup_token(parts[-1], limit=1)
        if sugg:
            corrected = " ".join([*parts[:-1], sugg[0].term])
            fuzzy_res = lookup_trie(corrected, index_state, max_completions=6, max_products=4)
            completions = list(fuzzy_res.completions)
            if not products:
                products = list(fuzzy_res.products)

    if layout_corrected and all(c.text.lower() != layout_corrected for c in completions):
        completions.insert(0, SuggestCompletion(text=layout_corrected, kind="layout_fix", score=2.0))
        completions = completions[:8]

    # Связанные запросы и chips для омонимов
    related: List[str] = []
    for c in completions[1:4]:
        if c.text.lower() != q_norm and c.text not in related:
            related.append(c.text)

    chips: List[IntentChip] = []
    if q_norm in _HOMONYM_CHIPS:
        chips = list(_HOMONYM_CHIPS[q_norm])
    elif not q_norm:
        chips = [
            IntentChip(label="Антивирусы для бизнеса", query_rewrite="антивирус для бизнеса", intent_hint="software"),
            IntentChip(label="Резервное копирование", query_rewrite="резервное копирование", intent_hint="software"),
            IntentChip(label="Отечественное ПО", query_rewrite="российский аналог по", intent_hint="software"),
        ]

    if segment == Segment.gov and not q_norm:
        related = ["astra linux", "криптопро", "мойофис"][:3]

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return SuggestResponse(
        prefix_raw=prefix_raw,
        layout_corrected=layout_corrected,
        completions=completions[:8],
        products=products[:4],
        related_queries=related[:3],
        intent_chips=chips[:4],
        latency_ms=latency_ms,
    )