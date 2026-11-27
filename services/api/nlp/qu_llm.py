"""S4 (LLM-эскалация): вызов LLMGateway (LLMRole.FAST) с closed-set сущностей,
кэшированием через qu_cache и безопасным fallback на qu_rules при сбое/таймауте/L1–L3.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from services.api.app.schemas import ClientContext, IntentChip, ProductClass, QuLlmOutput
from services.api.core.cache import make_cache_key, make_ctx_bucket
from services.api.core.cost_meter import CostMeterAccumulator
from services.api.core.degradation import DegradationManager, get_degradation_manager
from services.api.llm.gateway import LLMGateway, LLMRole, get_gateway, wrap_user_input
from services.api.nlp.config import NlpConfig, get_nlp_config
from services.api.nlp.lexicon import EntityHit, EntityRef, Lexicon
from services.api.nlp.normalize import NormalizedQuery
from services.api.nlp.qu_rules import QuResult

log = logging.getLogger("smart_search.nlp.qu_llm")

PROMPT_VERSION = "qu_v1"
_VALID_CLASSES = frozenset(c.value for c in ProductClass)
_DEFAULT_SYSTEM = (
    "Ты модуль понимания запросов B2B-магазина ПО и ИТ-оборудования. "
    "Верни компактный JSON в одну строку с полями: "
    "intent (str), type_probs ({ProductClass: float}), entity_ids (только из candidates или []), "
    "attr_filters (dict), rewrites (1..3 str), "
    'clarification_chips (0..3 {"label":str,"query_rewrite":str,"intent_hint":str|null}), '
    "confidence (0..1)."
)


@lru_cache(maxsize=4)
def load_qu_prompt(path_override: Optional[str] = None) -> str:
    candidates: List[Path] = []
    if path_override:
        candidates.append(Path(path_override))
    env_path = os.getenv("QU_PROMPT_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        Path("prompts/qu/v1.md"),
        Path(__file__).resolve().parents[3] / "prompts" / "qu" / "v1.md",
    ])
    for p in candidates:
        if p.is_file():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
    log.warning("prompts/qu/v1.md not found, using compact built-in fallback prompt")
    return _DEFAULT_SYSTEM


def _build_user_message(
    nq: NormalizedQuery,
    rules_res: QuResult,
    cfg: NlpConfig,
) -> Tuple[str, Set[str]]:
    candidates: Sequence[EntityRef] = rules_res.candidate_entities[: cfg.qu.max_candidate_entities]
    allowed_ids = {c.entity_id for c in candidates}

    cand_lines = [f"{c.entity_id}|{c.kind}|{c.canonical_name}" for c in candidates]
    types_hint = [
        f"{tid}:{cfg.types[tid].title_ru}"
        for tid in rules_res.predicted_types[:8]
        if tid in cfg.types
    ]
    parts = [
        f"classes: {', '.join(cfg.product_classes or sorted(_VALID_CLASSES))}",
        f"types_hint: {', '.join(types_hint) if types_hint else '-'}",
        f"candidates:\n" + ("\n".join(cand_lines) if cand_lines else "(empty)"),
        f"query: {wrap_user_input(nq.query_norm or nq.raw)}",
    ]
    return "\n".join(parts), allowed_ids


def _make_post_validator(allowed_ids: Set[str]):
    def _validate(out: QuLlmOutput) -> QuLlmOutput:
        filtered_ids = [eid for eid in dict.fromkeys(out.entity_ids) if eid in allowed_ids]
        clean_probs = {k: max(0.0, float(v)) for k, v in out.type_probs.items() if k in _VALID_CLASSES and float(v) > 0.0}
        total = sum(clean_probs.values())
        if total > 0.0:
            clean_probs = {k: round(v / total, 4) for k, v in sorted(clean_probs.items(), key=lambda kv: -kv[1])}
        else:
            clean_probs = {ProductClass.software.value: 0.7, ProductClass.other.value: 0.3}
        clean_rewrites = [r.strip() for r in out.rewrites if r and r.strip()][:3]
        clean_chips = [
            IntentChip(
                label=ch.label.strip(),
                query_rewrite=ch.query_rewrite.strip(),
                intent_hint=ch.intent_hint if ch.intent_hint in _VALID_CLASSES else None,
            )
            for ch in out.clarification_chips
            if ch.label.strip() and ch.query_rewrite.strip()
        ][:4]
        return out.model_copy(update={
            "entity_ids": filtered_ids,
            "type_probs": clean_probs,
            "rewrites": clean_rewrites,
            "clarification_chips": clean_chips,
        })

    return _validate


def _merge_llm_into_rules(
    rules_res: QuResult,
    llm_out: QuLlmOutput,
    lexicon: Lexicon,
    allowed_ids: Set[str],
    *,
    from_cache: bool,
) -> QuResult:
    valid_ids = [eid for eid in llm_out.entity_ids if eid in allowed_ids]
    hits: List[EntityHit] = list(rules_res.entity_hits)
    existing_ids = {h.ref.entity_id for h in hits}
    for eid in valid_ids:
        if eid not in existing_ids:
            ref = lexicon.entity_by_id.get(eid) or next((c for c in rules_res.candidate_entities if c.entity_id == eid), None)
            if ref is not None:
                hits.append(EntityHit(ref=ref, start=0, end=0, surface=ref.canonical_name, via="llm_closed_set", confidence=round(llm_out.confidence, 4)))
                existing_ids.add(eid)

    rewrites: List[str] = list(rules_res.rewrites)
    for r in llm_out.rewrites:
        if r not in rewrites:
            rewrites.append(r)

    chips: List[IntentChip] = list(llm_out.clarification_chips) or list(rules_res.clarification_chips)

    return QuResult(
        intent=llm_out.intent or rules_res.intent,
        type_probs=llm_out.type_probs or rules_res.type_probs,
        predicted_types=rules_res.predicted_types,
        entity_hits=tuple(hits),
        candidate_entities=rules_res.candidate_entities,
        attrs=rules_res.attrs,
        rewrites=tuple(rewrites[:3]),
        clarification_chips=tuple(chips[:4]),
        confidence=round(max(rules_res.confidence, llm_out.confidence), 4),
        is_bare_noun=rules_res.is_bare_noun,
        is_homonym=rules_res.is_homonym,
        is_out_of_catalog_hint=rules_res.is_out_of_catalog_hint,
        escalate_to_llm=rules_res.escalate_to_llm,
        escalation_reasons=rules_res.escalation_reasons,
        source="cache" if from_cache else "llm",
    )


async def escalate_qu_llm(
    nq: NormalizedQuery,
    rules_res: QuResult,
    lexicon: Lexicon,
    *,
    ctx: Optional[ClientContext] = None,
    overlay_version: int = 0,
    trace_id: Optional[str] = None,
    llm_enabled: bool = True,
    force: bool = False,
    gateway: Optional[LLMGateway] = None,
    degradation: Optional[DegradationManager] = None,
    meter: Optional[CostMeterAccumulator] = None,
    cfg: Optional[NlpConfig] = None,
) -> QuResult:
    """Вызывает LLM для уточнения QU, если нужна эскалация и разрешено режимом деградации."""
    cfg = cfg or get_nlp_config()
    if nq.is_empty or (not rules_res.escalate_to_llm and not force):
        return rules_res

    deg = degradation or get_degradation_manager()
    if not deg.allow_llm(requested=llm_enabled):
        return QuResult(**{**rules_res.__dict__, "source": "fallback_rules"})

    gw = gateway or get_gateway()
    ctx_bucket = make_ctx_bucket(ctx.segment.value if ctx else None, ctx.page_category_id if ctx else None)
    cache_key = make_cache_key(nq.query_norm, ctx_bucket, overlay_version, namespace="qu", extra=PROMPT_VERSION)

    system_prompt = load_qu_prompt()
    user_prompt, allowed_ids = _build_user_message(nq, rules_res, cfg)

    res = await gw.complete_json(
        purpose="qu",
        role=LLMRole.FAST,
        system=system_prompt,
        user=user_prompt,
        schema=QuLlmOutput,
        trace_id=trace_id,
        max_tokens=cfg.qu.max_tokens_out,
        temperature=0.0,
        timeout_ms=cfg.qu.llm_timeout_ms,
        cache_key=cache_key,
        use_cache=True,
        allow_fallback=True,
        post_validate=_make_post_validator(allowed_ids),
        meter=meter,
    )
    if not res.ok or res.parsed is None:
        log.info("qu_llm fallback to rules (trace_id=%s err=%s)", trace_id, res.error)
        return QuResult(**{**rules_res.__dict__, "source": "fallback_rules"})

    return _merge_llm_into_rules(rules_res, res.parsed, lexicon, allowed_ids, from_cache=res.from_cache)