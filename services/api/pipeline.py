"""Оркестратор полного поиска S0–S12 (CONTRACTS.md B.4, B.8, B.9) и dry-run probe."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Union

from services.api.app.schemas import (
    ApiError,
    DataTier,
    DegradationLevel,
    EventType,
    OverlayRulePayload,
    SearchRequest,
    SearchResponse,
    StepId,
    TraceStep,
)
from services.api.core.cost_meter import CostMeterAccumulator
from services.api.core.degradation import DegradationManager, get_degradation_manager
from services.api.core.events import EventStore, get_event_store, mask_pii, new_trace_id
from services.api.core.index_state import IndexState, get_index_state
from services.api.core.overlay import OverlayItem, OverlaySnapshot, OverlayStore, get_overlay_store, make_item_id, normalize_key
from services.api.core.trace import TraceCollector, format_sse, format_sse_span
from services.api.growth import build_cross_sell
from services.api.legacy import run_legacy_search
from services.api.llm.gateway import LLMGateway, get_gateway
from services.api.nlp import NlpAnalysis, generate_hypotheses, get_lexicon, get_nlp_config, get_symspell_dict, normalize_query, tokenize
from services.api.nlp.qu_llm import escalate_qu_llm
from services.api.nlp.qu_rules import analyze_qu_rules
from services.api.ranking import (
    apply_guarantee_ladder,
    apply_mmr,
    compose_sections,
    materialize_card,
    score_candidates,
)
from services.api.retrieval import run_retrieval

log = logging.getLogger("smart_search.pipeline")


def _build_temp_overlay(
    base: OverlaySnapshot,
    patch: Union[OverlayRulePayload, Sequence[OverlayRulePayload], Dict[str, Any], None],
) -> OverlaySnapshot:
    """Накладывает временные правила в памяти для dry-run `/internal/probe` без записи в SQLite."""
    if patch is None:
        return base
    patches = list(patch) if isinstance(patch, (list, tuple)) else [patch]
    items_map: Dict[tuple, OverlayItem] = {(it.kind, it.key_norm): it for it in base.items if it.active}
    for p in patches:
        if isinstance(p, dict):
            p = OverlayRulePayload.model_validate(p)
        kn = normalize_key(p.key)
        if not kn:
            continue
        item = OverlayItem(
            item_id=make_item_id(p.kind, kn),
            kind=p.kind,
            key_norm=kn,
            value=dict(p.value),
            active=True,
            config_version=base.config_version,
            updated_at=base.created_at,
        )
        items_map[(p.kind, kn)] = item
    return OverlaySnapshot(
        config_version=base.config_version,
        created_at=base.created_at,
        items=tuple(items_map.values()),
        ranking_weights=dict(base.ranking_weights),
    )


async def execute_search(
    req: SearchRequest,
    *,
    trace_id: Optional[str] = None,
    source: str = "live",
    persist_events: bool = True,
    index_state: Optional[IndexState] = None,
    overlay_store: Optional[OverlayStore] = None,
    overlay_override: Optional[OverlaySnapshot] = None,
    degradation: Optional[DegradationManager] = None,
    gateway: Optional[LLMGateway] = None,
    event_store: Optional[EventStore] = None,
    on_step: Any = None,
) -> SearchResponse:
    tid = trace_id or new_trace_id()
    idx = index_state or get_index_state()
    ov_store = overlay_store or get_overlay_store()
    deg = degradation or get_degradation_manager()
    gw = gateway or get_gateway()
    ev_store = event_store or get_event_store()
    cfg = get_nlp_config()

    if overlay_override is None and deg.overlay_allowed():
        ov_store.maybe_refresh()
        overlay = ov_store.snapshot()
    elif overlay_override is not None:
        overlay = overlay_override
    else:
        overlay = ov_store.base_snapshot()

    deg_level = deg.level()
    allow_personalization = deg.overlay_allowed()

    # Режим Legacy (P0)
    if req.options.mode == "legacy":
        resp = run_legacy_search(
            req.query,
            ctx=req.context,
            index_state=idx,
            config_version=overlay.config_version,
            degradation=deg_level,
            limit=req.options.limit,
            include_trace=req.options.include_trace,
            trace_id=tid,
        )
        if on_step is not None:
            for st in resp.trace:
                res = on_step(st)
                if asyncio.iscoroutine(res):
                    await res
        if persist_events and deg.db_allowed():
            asyncio.create_task(ev_store.awrite_search_trace(resp, session_id=req.session_id, source=source))
        return resp

    trace = TraceCollector(on_step=on_step, enabled=req.options.include_trace)
    meter = CostMeterAccumulator()

    # S0. Приём и маскирование PII
    with trace.step(StepId.S0) as st:
        query_safe = mask_pii(req.query)
        st.set_summary(f"Запрос принят (сегмент={req.context.segment.value}, уровень={deg_level.value})")
        st.add_data(segment=req.context.segment.value, degradation=deg_level.value, pii_masked=(query_safe != req.query))

    # S1. Нормализация, раскладка, гомоглифы
    with trace.step(StepId.S1) as st:
        nq = normalize_query(query_safe, cfg)
        st.set_summary(f"norm='{nq.query_norm}', шумовые={list(nq.noise_tokens)}")
        st.add_data(query_norm=nq.query_norm, noise_tokens=list(nq.noise_tokens), homoglyphs=list(nq.homoglyph_fixes))

    # S2. Токенизация и лемматизация
    with trace.step(StepId.S2) as st:
        tq = tokenize(nq, cfg)
        st.set_summary(f"токенов={len(tq)}, леммы={list(tq.lemmas[:6])}")
        st.add_data(tokens=list(tq.texts), lemmas=list(tq.lemmas), versions=list(tq.versions), abbrevs=list(tq.abbreviations))

    # S3. Гипотезы написания
    with trace.step(StepId.S3) as st:
        lex = get_lexicon(cfg, idx, overlay)
        sym = get_symspell_dict(cfg, lex, idx, overlay)
        hyps = generate_hypotheses(nq, tq, lex, sym, cfg)
        st.set_summary(
            f"гипотез={len(hyps.hypotheses)}, applied={hyps.applied_query!r}, dym={hyps.did_you_mean!r}"
        )
        if hyps.applied_query:
            st.set_decision(f"auto:{hyps.correction_source}")
        elif hyps.protected_exact:
            st.set_decision("protected_exact")
        st.add_data(
            applied_query=hyps.applied_query,
            did_you_mean=hyps.did_you_mean,
            protected_exact=hyps.protected_exact,
            top_hypotheses=[{"q": h.query, "src": h.source, "conf": h.confidence} for h in hyps.hypotheses[:4]],
        )

    # S4. Понимание запроса (QU: правила + при необходимости LLM)
    async with trace.astep(StepId.S4) as st:
        qu_rules_res = analyze_qu_rules(nq, tq, lex, hyps, ctx=req.context, cfg=cfg)
        qu_res = await escalate_qu_llm(
            nq,
            qu_rules_res,
            lex,
            ctx=req.context,
            overlay_version=overlay.config_version,
            trace_id=tid,
            llm_enabled=req.options.llm_enabled,
            gateway=gw,
            degradation=deg,
            meter=meter,
            cfg=cfg,
        )
        analysis = NlpAnalysis(normalized=nq, tokenized=tq, lexicon=lex, hypotheses=hyps, qu=qu_res)
        if qu_res.source == "fallback_rules":
            st.fallback("LLM недоступен/ошибка, использованы правила", decision="fallback_rules")
        else:
            st.set_decision(qu_res.source)
            st.set_summary(f"intent={qu_res.intent}, conf={qu_res.confidence}, src={qu_res.source}")
        st.add_data(
            intent=qu_res.intent,
            type_probs=qu_res.type_probs,
            attrs=qu_res.attrs.to_dict(),
            escalated=qu_res.escalate_to_llm,
            escalation_reasons=list(qu_res.escalation_reasons),
            source=qu_res.source,
        )

    # S5. Retrieval L1..L5 + RRF Fusion
    with trace.step(StepId.S5) as st:
        ret = await asyncio.to_thread(
            run_retrieval,
            analysis,
            idx,
            ctx=req.context,
            disable_dense=req.options.disable_dense,
            degradation=deg,
        )
        if ret.dense_fallback_l2:
            st.fallback(
                f"пул={len(ret.candidates)} (без dense L4), hard_null={ret.hard_null}",
                decision="degraded_l2",
            )
        else:
            st.set_summary(f"пул={len(ret.candidates)}, полосы={ret.lane_counts}, hard_null={ret.hard_null}")
        st.add_data(lane_counts=ret.lane_counts, hard_null=ret.hard_null, nav_hits=len(ret.nav_hits))

    # Актуализируем уровень деградации (мог перейти в L1/L2 на S4/S5)
    deg_level = deg.level()

    # S6. Контекст и персонализация (±15%)
    with trace.step(StepId.S6) as st:
        if not allow_personalization:
            st.skip("L3: персонализация отключена")
        else:
            st.set_summary(f"сегмент={req.context.segment.value}, cat={req.context.page_category_id}, hist={len(req.context.clicked_family_ids)}")
            st.add_data(segment=req.context.segment.value, page_category_id=req.context.page_category_id)

    # S7. Ранжирование и overlay
    with trace.step(StepId.S7) as st:
        scored = score_candidates(
            ret.candidates,
            analysis,
            req.context,
            idx,
            overlay=overlay,
            allow_personalization=allow_personalization,
        )
        st.set_summary(f"оценено={len(scored)}, top1={scored[0].score if scored else 0.0}")
        st.add_data(config_version=overlay.config_version, top_fids=[s.candidate.family_id for s in scored[:5]])

    # S8 + S9. MMR и выбор вариантов (family -> offer)
    is_nav = analysis.qu.intent == "exact_entity" and not analysis.qu.is_homonym
    with trace.step(StepId.S8) as st:
        mmr_selected = apply_mmr(scored, is_navigational=is_nav, limit=req.options.limit)
        weights = dict(overlay.ranking_weights)
        primary_cards = [c for c in (materialize_card(s, analysis, req.context, idx, weights) for s in mmr_selected) if c is not None]
        st.set_summary(f"варианты выбраны для {len(primary_cards)} family")

    with trace.step(StepId.S9) as st:
        st.set_summary(f"MMR={'выкл (навигационный)' if is_nav else 'вкл'}, кандидатов={len(primary_cards)}")
        st.set_decision("no_mmr" if is_nav else "mmr")

    # S10. Лестница гарантии >= 4 family + сборка секций
    with trace.step(StepId.S10) as st:
        remaining = [s for s in scored if s not in mmr_selected]
        ladder_res = apply_guarantee_ladder(
            primary_cards,
            remaining,
            analysis,
            req.context,
            idx,
            weights,
            hard_null=ret.hard_null,
        )
        sections = compose_sections(
            ladder_res.cards,
            analysis,
            out_of_catalog_banner=ret.out_of_catalog_banner,
        )
        shown_cards = [it for sec in sections for it in sec.items]
        if ladder_res.fill_rung > 0:
            st.fallback(f"лестница ступень={ladder_res.fill_rung}, итого={len(shown_cards)} family", decision=f"rung_{ladder_res.fill_rung}")
        else:
            st.set_summary(f"ступень=0 (достаточно), секций={len(sections)}, итого={len(shown_cards)} family")
        st.add_data(fill_rung=ladder_res.fill_rung, soft_null=ladder_res.soft_null, n_sections=len(sections))

    # S11. Допродажи (cross-sell)
    with trace.step(StepId.S11) as st:
        shown_fids = {c.family_id for c in shown_cards}
        anchors = [c.family_id for c in shown_cards[:4]]
        cross_sell = build_cross_sell(
            anchors,
            shown_fids,
            req.context,
            idx,
            allow_analog=bool(ret.out_of_catalog_banner or "analog" in analysis.qu.attrs.modifiers),
        )
        st.set_summary(f"допродажи={len(cross_sell)}")
        st.add_data(cross_sell_fids=[c.family_id for c in cross_sell])

    # S12. Сборка ответа и запись события
    with trace.step(StepId.S12) as st:
        st.set_summary("Ответ собран")

    meter.set_cpu_ms_total(trace.cpu_ms_total)
    cost_meter = meter.build(data_tier=DataTier.derived)

    related_queries: List[str] = list(analysis.qu.rewrites[:3])
    for exp in analysis.hypotheses.expanded_terms:
        if exp not in related_queries and len(related_queries) < 4:
            related_queries.append(exp)

    response = SearchResponse(
        trace_id=tid,
        query_raw=query_safe,
        query_norm=nq.query_norm,
        applied_query=hyps.applied_query,
        did_you_mean=hyps.did_you_mean,
        mode="smart",
        degradation_level=deg_level,
        config_version=overlay.config_version,
        index_version=idx.index_version,
        hard_null=ret.hard_null,
        soft_null=ladder_res.soft_null,
        fill_rung=ladder_res.fill_rung,
        out_of_catalog_banner=ret.out_of_catalog_banner,
        nav_hits=ret.nav_hits,
        intent_chips=list(analysis.qu.clarification_chips),
        sections=sections,
        cross_sell=cross_sell,
        related_queries=related_queries[:4],
        trace=trace.to_response(include=req.options.include_trace),
        cost_meter=cost_meter,
    )

    if persist_events and deg.db_allowed():
        async def _persist() -> None:
            await ev_store.awrite_search_trace(response, session_id=req.session_id, source=source)
            await ev_store.awrite_event(
                EventType.search_issued,
                req.session_id,
                trace_id=tid,
                source=source,
                config_version=overlay.config_version,
                payload={
                    "query_norm": nq.query_norm,
                    "hard_null": ret.hard_null,
                    "soft_null": ladder_res.soft_null,
                    "fill_rung": ladder_res.fill_rung,
                    "degradation_level": deg_level.value,
                    "cost_micro_rub": cost_meter.cost_micro_rub,
                },
            )

        asyncio.create_task(_persist())

    return response


async def execute_search_stream(
    req: SearchRequest,
    *,
    trace_id: Optional[str] = None,
    source: str = "live",
) -> AsyncIterator[str]:
    """SSE-генератор: шлёт `event: span` по мере завершения S0–S12 и завершает `event: final` (B.9)."""
    queue: asyncio.Queue = asyncio.Queue()
    tid = trace_id or new_trace_id()

    def _on_step(step: TraceStep) -> None:
        queue.put_nowait(("span", step))

    async def _runner() -> None:
        try:
            resp = await execute_search(req, trace_id=tid, source=source, on_step=_on_step)
            await queue.put(("final", resp))
        except Exception as exc:  # noqa: BLE001
            log.exception("SSE search failed (trace_id=%s)", tid)
            await queue.put(("error", ApiError(error_code="search_error", message=str(exc)[:200], trace_id=tid)))
        finally:
            await queue.put(None)

    task = asyncio.create_task(_runner())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            kind, payload = item
            if kind == "span":
                yield format_sse_span(payload)
            else:
                yield format_sse(kind, payload)
    finally:
        if not task.done():
            task.cancel()


async def run_probe(
    query: str,
    temp_patch: Any = None,
    *,
    req_options: Optional[Dict[str, Any]] = None,
) -> SearchResponse:
    """Dry-run прогон для `POST /internal/probe` (без записи событий и мутации БД)."""
    ov_store = get_overlay_store()
    ov_store.maybe_refresh()
    temp_overlay = _build_temp_overlay(ov_store.snapshot(), temp_patch)
    req = SearchRequest(query=query, session_id="ss_probe", options=req_options or {"llm_enabled": False})
    return await execute_search(
        req,
        persist_events=False,
        overlay_override=temp_overlay,
        source="gold_replay",
    )