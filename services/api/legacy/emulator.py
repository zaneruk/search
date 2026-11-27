"""Legacy-эмулятор (P0 в CONTRACTS.md A.9): наивный поиск по подстрокам без морфологии,
без разделения name_head/name_target, без fuzzy, транслита, синонимов, QU и лестницы >=4.

Воспроизводит описанную в ТЗ проблему: по запросу «сервер» выдает ПО «для сервера»,
а запросы с опечатками/пробелами/раскладкой дают hard_null=True.
"""
from __future__ import annotations

import time
from typing import List, Optional

from services.api.app.schemas import (
    ClientContext,
    CostMeter,
    DataTier,
    DegradationLevel,
    ProductCard,
    ProductClass,
    RelevanceLabel,
    SearchResponse,
    SearchSection,
    StepId,
    TraceStep,
)
from services.api.core.events import new_trace_id
from services.api.core.index_state import IndexState
from services.api.growth.promo import build_badges
from services.api.nlp.config import basic_norm
from services.api.nlp.normalize import normalize_query
from services.api.nlp.tokenize import tokenize
from services.api.nlp import NlpAnalysis
from services.api.nlp.hypotheses import HypothesesResult
from services.api.nlp.lexicon import Lexicon, get_lexicon
from services.api.nlp.qu_rules import ExtractedAttrs, QuResult
from services.api.ranking.variants import resolve_variants


def run_legacy_search(
    query: str,
    *,
    ctx: ClientContext,
    index_state: IndexState,
    config_version: int = 0,
    degradation: DegradationLevel = DegradationLevel.L0,
    limit: int = 20,
    include_trace: bool = True,
    trace_id: Optional[str] = None,
) -> SearchResponse:
    t0 = time.perf_counter()
    c0 = time.thread_time()
    tid = trace_id or new_trace_id()
    q_norm = basic_norm(query)
    tokens = [t for t in q_norm.split() if len(t) >= 2]

    cards: List[ProductCard] = []
    if index_state.is_loaded and index_state.families is not None and tokens:
        f_df = index_state.families
        cnames = f_df["canonical_name"].fillna("").astype(str).tolist()
        vendors = f_df["vendor"].fillna("").astype(str).tolist()
        fids = f_df["family_id"].astype(str).tolist()
        pclasses = f_df["product_class"].fillna("other").astype(str).tolist()
        ptypes = f_df["product_type"].fillna("").astype(str).tolist()
        ru_flags = f_df["is_ru_registry"].fillna(False).astype(bool).tolist() if "is_ru_registry" in f_df.columns else [False] * len(f_df)

        matches = []
        for idx, cname in enumerate(cnames):
            hay = f"{cname} {vendors[idx]}".lower()
            if all(tok in hay for tok in tokens):
                # Наивное предпочтение по длине имени/вхождению, без учёта «для сервера» и без наличия
                score = sum(hay.count(tok) for tok in tokens) * 0.5 + (1.0 if q_norm in hay else 0.0)
                matches.append((score, idx))

        matches.sort(key=lambda x: (-x[0], x[1]))

        # Минимальный NlpAnalysis только для вызова resolve_variants
        nq = normalize_query(query)
        tq = tokenize(nq)
        stub_analysis = NlpAnalysis(
            normalized=nq,
            tokenized=tq,
            lexicon=get_lexicon(index_state=index_state),
            hypotheses=HypothesesResult(original=q_norm),
            qu=QuResult(
                intent="legacy",
                type_probs={},
                predicted_types=(),
                entity_hits=(),
                candidate_entities=(),
                attrs=ExtractedAttrs(),
            ),
        )

        for sc, pos in matches[:limit]:
            fid = fids[pos]
            var = resolve_variants(fid, stub_analysis, ctx, index_state)
            if var is None:
                continue
            try:
                pcls = ProductClass(pclasses[pos])
            except ValueError:
                pcls = ProductClass.other
            cards.append(
                ProductCard(
                    family_id=fid,
                    canonical_name=cnames[pos],
                    vendor=vendors[pos],
                    product_class=pcls,
                    product_type=ptypes[pos],
                    representative=var.representative,
                    n_variants=var.n_variants,
                    variant_axes=var.variant_axes,
                    price_min_minor=var.price_min_minor,
                    badges=build_badges(var.representative, is_ru_registry=ru_flags[pos]),
                    relevance_label=RelevanceLabel.exact if q_norm in cnames[pos].lower() else RelevanceLabel.close,
                    matched_fields=["legacy_substring"],
                    score=round(sc, 4),
                )
            )

    ms = round((time.perf_counter() - t0) * 1000.0, 3)
    cpu_ms = round((time.thread_time() - c0) * 1000.0, 3)
    hard_null = len(cards) == 0

    sections = (
        [SearchSection(section_id="sec_legacy", title="Результаты поиска (Legacy)", items=cards)]
        if cards
        else [SearchSection(section_id="sec_legacy", title="Результаты поиска (Legacy)", empty_reason="По точному совпадению ничего не найдено", items=[])]
    )

    trace = (
        [
            TraceStep(
                id=StepId.S5,
                title="Legacy-поиск (подстрока без морфологии и лестницы)",
                status="ok",
                ms=ms,
                cpu_ms=cpu_ms,
                summary=f"Найдено {len(cards)} family по точному вхождению токенов",
                decision="legacy_mode",
                fallback_used=False,
                data={"tokens": tokens, "hard_null": hard_null},
            )
        ]
        if include_trace
        else []
    )

    return SearchResponse(
        trace_id=tid,
        query_raw=query,
        query_norm=q_norm,
        mode="legacy",
        degradation_level=degradation,
        config_version=config_version,
        index_version=index_state.index_version,
        hard_null=hard_null,
        soft_null=hard_null,
        fill_rung=0,
        sections=sections,
        trace=trace,
        cost_meter=CostMeter(
            llm_calls=0,
            tokens_in=0,
            tokens_cached=0,
            tokens_out=0,
            cpu_ms_total=cpu_ms,
            cost_micro_rub=0,
            data_tier=DataTier.derived,
        ),
    )