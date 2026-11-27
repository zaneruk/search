"""Единый фасад шагов S1–S4 (нормализация, токенизация, гипотезы, QU)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.api.app.schemas import ClientContext
from services.api.core.cost_meter import CostMeterAccumulator
from services.api.core.degradation import DegradationManager
from services.api.llm.gateway import LLMGateway
from services.api.nlp.config import NlpConfig, get_nlp_config
from services.api.nlp.hypotheses import HypothesesResult, SpellingHypothesis, generate_hypotheses
from services.api.nlp.layout import LayoutFixer, convert_en_to_ru, convert_ru_to_en
from services.api.nlp.lexicon import EntityHit, EntityRef, Lexicon, get_lexicon
from services.api.nlp.normalize import NormalizedQuery, normalize_query, normalize_text
from services.api.nlp.qu_llm import escalate_qu_llm
from services.api.nlp.qu_rules import ExtractedAttrs, QuResult, analyze_qu_rules
from services.api.nlp.symspell_dict import SpellSuggestion, SymSpellDict, get_symspell_dict
from services.api.nlp.tokenize import Token, TokenizedQuery, lemmatize_phrase, lemmatize_word, tokenize
from services.api.nlp.translit import TranslitIndex, phonetic_keys, same_sound


@dataclass(frozen=True)
class NlpAnalysis:
    normalized: NormalizedQuery
    tokenized: TokenizedQuery
    lexicon: Lexicon
    hypotheses: HypothesesResult
    qu: QuResult


def analyze_query_rules(
    query: str,
    *,
    ctx: Optional[ClientContext] = None,
    index_state: Any = None,
    overlay: Any = None,
    cfg: Optional[NlpConfig] = None,
) -> NlpAnalysis:
    """Синхронный детерминированный анализ S1–S4 (работает в L0–L3, без LLM)."""
    cfg = cfg or get_nlp_config()
    nq = normalize_query(query, cfg)
    tq = tokenize(nq, cfg)
    lex = get_lexicon(cfg, index_state, overlay)
    sym = get_symspell_dict(cfg, lex, index_state, overlay)
    hyps = generate_hypotheses(nq, tq, lex, sym, cfg)
    qu = analyze_qu_rules(nq, tq, lex, hyps, ctx=ctx, cfg=cfg)
    return NlpAnalysis(normalized=nq, tokenized=tq, lexicon=lex, hypotheses=hyps, qu=qu)


async def analyze_query(
    query: str,
    *,
    ctx: Optional[ClientContext] = None,
    index_state: Any = None,
    overlay: Any = None,
    trace_id: Optional[str] = None,
    llm_enabled: bool = True,
    force_llm: bool = False,
    gateway: Optional[LLMGateway] = None,
    degradation: Optional[DegradationManager] = None,
    meter: Optional[CostMeterAccumulator] = None,
    cfg: Optional[NlpConfig] = None,
) -> NlpAnalysis:
    """Полный анализ S1–S4, включая LLM-эскалацию при необходимости."""
    base = analyze_query_rules(query, ctx=ctx, index_state=index_state, overlay=overlay, cfg=cfg)
    overlay_version = int(getattr(overlay, "config_version", 0))
    qu = await escalate_qu_llm(
        base.normalized,
        base.qu,
        base.lexicon,
        ctx=ctx,
        overlay_version=overlay_version,
        trace_id=trace_id,
        llm_enabled=llm_enabled,
        force=force_llm,
        gateway=gateway,
        degradation=degradation,
        meter=meter,
        cfg=cfg,
    )
    return NlpAnalysis(
        normalized=base.normalized,
        tokenized=base.tokenized,
        lexicon=base.lexicon,
        hypotheses=base.hypotheses,
        qu=qu,
    )


__all__ = [
    "EntityHit",
    "EntityRef",
    "ExtractedAttrs",
    "HypothesesResult",
    "LayoutFixer",
    "Lexicon",
    "NlpAnalysis",
    "NlpConfig",
    "NormalizedQuery",
    "QuResult",
    "SpellSuggestion",
    "SpellingHypothesis",
    "SymSpellDict",
    "Token",
    "TokenizedQuery",
    "TranslitIndex",
    "analyze_query",
    "analyze_query_rules",
    "analyze_qu_rules",
    "convert_en_to_ru",
    "convert_ru_to_en",
    "escalate_qu_llm",
    "generate_hypotheses",
    "get_lexicon",
    "get_nlp_config",
    "get_symspell_dict",
    "lemmatize_phrase",
    "lemmatize_word",
    "normalize_query",
    "normalize_text",
    "phonetic_keys",
    "same_sound",
    "tokenize",
]