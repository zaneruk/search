"""S4 (быстрый детерминированный путь): разметка сущностей, атрибутов, голых существительных,
распределение вероятностей ProductClass и решение об эскалации к LLM.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from services.api.app.schemas import ClientContext, IntentChip, ProductClass
from services.api.nlp.config import NlpConfig, basic_norm, get_nlp_config
from services.api.nlp.hypotheses import HypothesesResult
from services.api.nlp.lexicon import EntityHit, EntityRef, Lexicon, get_lexicon
from services.api.nlp.normalize import NormalizedQuery
from services.api.nlp.tokenize import TokenizedQuery, tokenize

_TERM_RE = re.compile(r"\b(\d{1,3})\s*(мес(?:яц(?:ев|а)?)?|год(?:а|ов)?|лет|г\.)\b", re.U)
_QTY_RE = re.compile(
    r"\b(\d{1,5})\s*(пользовател(?:ей|я)?|лицензи(?:й|и|я)?|пк|рабоч\w*\s+мест\w*|мест\w*|устройств\w*|узл(?:ов|а)?|сервер(?:ов|а)?|аккаунт(?:ов|а)?|юзер(?:ов|а)?|users?|seats?|devices?|nodes?)\b",
    re.U,
)
_RELATION_TOKENS = frozenset({"для", "под", "compatible", "for"})
_ANALOG_FALLBACK = ("аналог", "замена", "альтернатива", "вместо", "analog")
_RU_FALLBACK = ("российский", "российская", "российское", "отечественный", "импортозамещение", "реестр", "рф")

_SEGMENT_FALLBACK: Mapping[str, Tuple[str, ...]] = {
    "smb": ("малый бизнес", "малого бизнеса", "для бизнеса", "ип", "смб", "smb", "офис"),
    "enterprise": ("корпоратив", "enterprise", "крупный бизнес", "крупного бизнеса", "датацентр", "цод"),
    "gov": ("гос", "госкомпан", "госсектор", "бюджет", "фз", "фстэк", "фсб", "gov"),
    "edu": ("вуз", "образован", "учебн", "школ", "университет", "студент", "преподавател", "academic", "edu"),
    "personal": ("для дома", "домашн", "физлиц", "личн", "семья", "family", "home", "personal"),
}
_LICENSE_FALLBACK: Mapping[str, Tuple[str, ...]] = {
    "subscription": ("подписка", "подписки", "аренда", "saas", "subscription"),
    "perpetual": ("бессрочн", "постоянн", "perpetual"),
    "renewal": ("продлен", "renewal"),
    "oem": ("oem",),
    "box": ("коробк", "box"),
}


@dataclass(frozen=True)
class ExtractedAttrs:
    term_months: Optional[int] = None
    qty_users: Optional[int] = None
    version: Optional[str] = None
    segment: Optional[str] = None
    license_type: Optional[str] = None
    modifiers: Tuple[str, ...] = ()
    relation: Optional[str] = None
    target_text: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        if self.term_months is not None:
            out["term_months"] = self.term_months
        if self.qty_users is not None:
            out["qty_users"] = self.qty_users
        if self.version is not None:
            out["version"] = self.version
        if self.segment is not None:
            out["segment"] = self.segment
        if self.license_type is not None:
            out["license_type"] = self.license_type
        if self.modifiers:
            out["modifiers"] = list(self.modifiers)
        if self.relation is not None:
            out["relation"] = self.relation
        if self.target_text is not None:
            out["target_text"] = self.target_text
        return out


@dataclass
class QuResult:
    intent: str
    type_probs: Dict[str, float]
    predicted_types: Tuple[str, ...]
    entity_hits: Tuple[EntityHit, ...]
    candidate_entities: Tuple[EntityRef, ...]
    attrs: ExtractedAttrs
    rewrites: Tuple[str, ...] = ()
    clarification_chips: Tuple[IntentChip, ...] = ()
    confidence: float = 0.0
    is_bare_noun: bool = False
    is_homonym: bool = False
    is_out_of_catalog_hint: bool = False
    escalate_to_llm: bool = False
    escalation_reasons: Tuple[str, ...] = ()
    source: str = "rules"


def _normalize_probs(raw: Mapping[str, float], classes: Sequence[str]) -> Dict[str, float]:
    valid = set(classes) or {c.value for c in ProductClass}
    cleaned = {k: max(0.0, float(v)) for k, v in raw.items() if k in valid and float(v) > 0.0}
    total = sum(cleaned.values())
    if total <= 0.0:
        return {ProductClass.software.value: 0.7, ProductClass.other.value: 0.3}
    return {k: round(v / total, 4) for k, v in sorted(cleaned.items(), key=lambda kv: -kv[1])}


def _extract_attrs(nq: NormalizedQuery, tq: TokenizedQuery, cfg: NlpConfig) -> Tuple[ExtractedAttrs, Set[int]]:
    text = nq.query_norm
    consumed: Set[int] = set()

    term_months: Optional[int] = None
    m_term = _TERM_RE.search(text)
    if m_term:
        val = int(m_term.group(1))
        unit = m_term.group(2)
        term_months = val * 12 if unit.startswith(("г", "л")) else val
        for idx, tok in enumerate(tq.tokens):
            if tok.start >= m_term.start() and tok.end <= m_term.end():
                consumed.add(idx)

    qty_users: Optional[int] = None
    m_qty = _QTY_RE.search(text)
    if m_qty:
        qty_users = int(m_qty.group(1))
        for idx, tok in enumerate(tq.tokens):
            if tok.start >= m_qty.start() and tok.end <= m_qty.end():
                consumed.add(idx)

    version = tq.versions[0] if tq.versions else None
    for idx, tok in enumerate(tq.tokens):
        if tok.kind == "version":
            consumed.add(idx)

    # Segment
    lemma_text = " ".join(tq.lemmas)
    haystacks = (text, lemma_text)

    # Segment
    segment: Optional[str] = None
    seg_groups = (
        cfg.marker_groups("segment")
        or cfg.marker_groups("segments")
        or cfg.marker_groups("segment_markers")
    )
    for seg_code, stems in _SEGMENT_FALLBACK.items():
        cfg_phrases = [" ".join(p) for p in seg_groups.get(seg_code, ())]
        phrases = list(dict.fromkeys([*cfg_phrases, *stems]))
        if any(p in h for p in phrases for h in haystacks):
            segment = seg_code
            break

    # License type
    license_type: Optional[str] = None
    lic_groups = cfg.marker_groups("license_type") or cfg.marker_groups("license_types")
    for lic_code, stems in _LICENSE_FALLBACK.items():
        cfg_phrases = [" ".join(p) for p in lic_groups.get(lic_code, ())]
        phrases = list(dict.fromkeys([*cfg_phrases, *stems]))
        if any(p in h for p in phrases for h in haystacks):
            license_type = lic_code
            break

    # Modifiers
    mods: List[str] = []
    mod_map = cfg.modifier_markers()
    for mod_name, phrases in mod_map.items():
        str_phrases = [" ".join(p) for p in phrases if p]
        if any(p in h for p in str_phrases for h in haystacks):
            mods.append(mod_name)
    analog_phrases = [" ".join(p) for p in cfg.marker_phrases("relational_intent", "analog_of")] or list(_ANALOG_FALLBACK)
    if any(p in h for p in analog_phrases for h in haystacks) and "analog" not in mods:
        mods.append("analog")
    ru_phrases = [" ".join(p) for p in cfg.marker_phrases("modifiers", "ru_registry")] or list(_RU_FALLBACK)
    if any(p in h for p in ru_phrases for h in haystacks) and "ru_registry" not in mods:
        mods.append("ru_registry")

    # Relation («для», «под») — не захватываем числовые/атрибутные хвосты
    relation: Optional[str] = None
    target_text: Optional[str] = None
    for idx, tok in enumerate(tq.tokens):
        if (
            tok.text in _RELATION_TOKENS
            and idx not in consumed
            and idx + 1 < len(tq.tokens)
            and (idx + 1) not in consumed
            and tq.tokens[idx + 1].kind not in ("number", "version")
        ):
            relation = tok.text
            target_text = " ".join(t.text for t in tq.tokens[idx + 1:])
            break

    return (
        ExtractedAttrs(
            term_months=term_months,
            qty_users=qty_users,
            version=version,
            segment=segment,
            license_type=license_type,
            modifiers=tuple(mods),
            relation=relation,
            target_text=target_text,
        ),
        consumed,
    )


def _select_candidate_entities(
    tq: TokenizedQuery,
    hyps: HypothesesResult,
    direct_hits: Sequence[EntityHit],
    lexicon: Lexicon,
    limit: int,
) -> List[EntityRef]:
    seen: Set[str] = set()
    out: List[EntityRef] = []

    def push(ref: EntityRef) -> None:
        if ref.entity_id not in seen and len(out) < limit:
            seen.add(ref.entity_id)
            out.append(ref)

    for hit in direct_hits:
        push(hit.ref)

    # Поиск сущностей по гипотезам написания (транслит, склейка, раскладка)
    for h in hyps.hypotheses:
        h_tq = tokenize(h.query)
        for hit in lexicon.find_entity_hits(h_tq):
            push(hit.ref)

    # Пересечение лемма-токенов по словарю сущностей
    for ref, _score in lexicon.entities_for_tokens(tq.lemmas, limit=limit):
        push(ref)
    for exp in hyps.expanded_terms:
        for ref, _score in lexicon.entities_for_tokens(tokenize(exp).lemmas, limit=limit):
            push(ref)
    return out


def analyze_qu_rules(
    nq: NormalizedQuery,
    tq: TokenizedQuery,
    lexicon: Optional[Lexicon] = None,
    hyps: Optional[HypothesesResult] = None,
    ctx: Optional[ClientContext] = None,
    cfg: Optional[NlpConfig] = None,
) -> QuResult:
    cfg = cfg or get_nlp_config()
    lexicon = lexicon or get_lexicon(cfg)
    priors = cfg.priors
    classes = cfg.product_classes or tuple(c.value for c in ProductClass)

    if nq.is_empty:
        return QuResult(
            intent="empty",
            type_probs={ProductClass.software.value: 1.0},
            predicted_types=(),
            entity_hits=(),
            candidate_entities=(),
            attrs=ExtractedAttrs(),
            confidence=0.0,
            escalate_to_llm=False,
        )

    attrs, consumed_idx = _extract_attrs(nq, tq, cfg)

    # Сущности по оригинальному запросу и по auto-гипотезе
    raw_hits = lexicon.find_entity_hits(tq)
    assigned_hits: List[EntityHit] = []
    for h in raw_hits:
        c = priors.alias_verified if h.ref.status == "verified" else (priors.alias_seed if h.ref.status == "seed" else priors.alias_auto)
        assigned_hits.append(EntityHit(h.ref, h.start, h.end, h.surface, h.via, c))

    if hyps is not None and hyps.applied_query and hyps.applied_query != nq.query_norm:
        hyp_tq = tokenize(hyps.applied_query, cfg)
        for h in lexicon.find_entity_hits(hyp_tq):
            if not any(eh.ref.entity_id == h.ref.entity_id for eh in assigned_hits):
                assigned_hits.append(EntityHit(h.ref, h.start, h.end, h.surface, f"hyp:{hyps.correction_source}", 0.88))

    # Проверка голого существительного («сервер») и head nouns
    content_tokens = [tok for i, tok in enumerate(tq.tokens) if i not in consumed_idx]
    content_lemmas = [tok.lemma for tok in content_tokens]

    scores: Dict[str, float] = defaultdict(float)
    predicted_types: List[str] = []
    confidence = 0.0
    intent = "catalog_search"
    chips: List[IntentChip] = []
    is_bare_noun = False
    is_homonym = False
    is_ooc_hint = any(h.ref.kind == "external_product" for h in assigned_hits)

    # Если есть relation («для сервера» / «под сервер») -> это ПО/решение для цели, а не сама цель
    has_relation = attrs.relation is not None
    if len(content_tokens) == 1 and not has_relation:
        head = content_lemmas[0]
        if head in cfg.head_noun_priors:
            hp = cfg.head_noun_priors[head]
            is_bare_noun = True
            for cls, prob in hp.class_probs.items():
                scores[cls] += float(prob)
            if hp.default_product_type:
                predicted_types.append(hp.default_product_type)
            confidence = max(confidence, priors.qu_type_head)
            intent = f"bare_head:{head}"
            # Мультиинтентные chips для омонима («сервер»: железо / аренда IaaS / ПО)
            ranked_cls = sorted(hp.class_probs.items(), key=lambda kv: -kv[1])
            if len(ranked_cls) >= 2 and ranked_cls[1][1] >= 0.15:
                is_homonym = True
                for cls_code, _p in ranked_cls[:3]:
                    title = cfg.class_titles.get(cls_code, cls_code)
                    chips.append(IntentChip(label=title, query_rewrite=f"{head} {title.lower()}", intent_hint=cls_code))

    # Совпадения по head_nouns (вне relation-хвоста или как дополнение)
    rel_pos = next((i for i, tok in enumerate(tq.tokens) if tok.text in _RELATION_TOKENS), len(tq.tokens))
    for i, tok in enumerate(tq.tokens):
        if i in consumed_idx:
            continue
        lem = tok.lemma
        if i > rel_pos:
            # Токен стоит ПОСЛЕ «для»: «антивирус для сервера» -> предмет до «для», а «сервер» лишь контекст
            continue
        if lem in cfg.head_noun_to_types:
            for tid in cfg.head_noun_to_types[lem]:
                tinfo = cfg.types.get(tid)
                if tid not in predicted_types:
                    predicted_types.append(tid)
                if tinfo is not None:
                    scores[tinfo.product_class] += 0.65
            confidence = max(confidence, priors.qu_type_head if i == 0 else 0.75)

    # Бренды и entity-хиты
    brand_hit_found = any(t.text in lexicon.brand_terms or t.lemma in lexicon.brand_terms for t in tq.tokens)
    for eh in assigned_hits:
        confidence = max(confidence, eh.confidence)
        if eh.ref.kind == "brand":
            brand_hit_found = True
            b_prior = lexicon.brand_class_prior.get(basic_norm(eh.ref.canonical_name))
            if b_prior:
                for cls, p in b_prior.items():
                    scores[cls] += float(p)
        elif eh.ref.kind == "content":
            scores[ProductClass.training_webinar.value] += 0.65
            is_homonym = True
        elif eh.ref.kind == "product_type" and eh.ref.entity_id in cfg.types:
            tinfo = cfg.types[eh.ref.entity_id]
            scores[tinfo.product_class] += 0.8
            if tinfo.type_id not in predicted_types:
                predicted_types.append(tinfo.type_id)
    if brand_hit_found:
        confidence = max(confidence, priors.qu_brand_hit)

    # Вклад глоссария (product_types, указанные у терминов)
    for start, end, surface, lemma in tq.ngrams(3):
        for probe in (surface, lemma):
            for entry in cfg.glossary_entries(probe):
                for tid in entry.product_types:
                    if tid not in predicted_types:
                        predicted_types.append(tid)
                    tinfo = cfg.types.get(tid)
                    if tinfo is not None:
                        scores[tinfo.product_class] += 0.75
                confidence = max(confidence, priors.qu_type_glossary)

    if len(tq.tokens) == 1 and nq.query_norm in cfg.numeric_aliases:
        confidence = max(confidence, 0.8)
        scores[ProductClass.subscription.value] += 0.55
        scores[ProductClass.software.value] += 0.45

    if has_relation and ProductClass.software.value not in scores:
        scores[ProductClass.software.value] += 0.75
        confidence = max(confidence, 0.7)

    # Корректное определение интента и защита класса ПО для аналогов
    if "analog" in attrs.modifiers:
        intent = "find_analog"
        if not any(h in nq.query_norm for h in ("сервер", "коммутатор", "роутер", "принтер", "мфу", "ибп", "ноутбук", "железо", "оборудование")):
            scores[ProductClass.software.value] += 2.0
            scores[ProductClass.hardware.value] = 0.0
    elif "ru_registry" in attrs.modifiers:
        intent = "find_ru_analog"
    elif assigned_hits and not is_homonym:
        intent = "exact_entity"
    elif predicted_types and not is_bare_noun:
        intent = "product_type"

    if not scores:
        if any(c != ProductClass.other.value for c in ()) or attrs.to_dict():
            confidence = max(confidence, priors.qu_attr_only)
        scores[ProductClass.software.value] = 0.65
        scores[ProductClass.hardware.value] = 0.20
        scores[ProductClass.cloud_service.value] = 0.15

    type_probs = _normalize_probs(scores, classes)
    if ctx is not None and ctx.segment.value == "gov" and "ru_registry" not in attrs.modifiers:
        pass  # персонализация учитывается на шаге S6, не искажает семантику запроса

    # Переформулировки
    rewrites: List[str] = []
    if hyps is not None:
        if hyps.applied_query:
            rewrites.append(hyps.applied_query)
        for exp in hyps.expanded_terms:
            if exp not in rewrites and len(rewrites) < 3:
                rewrites.append(exp)

    # Кандидаты в closed-set для LLM (<= max_candidate_entities)
    candidates = tuple(_select_candidate_entities(tq, hyps or HypothesesResult(nq.query_norm), assigned_hits, lexicon, cfg.qu.max_candidate_entities))

    # Решение об эскалации к LLM (S4)
    reasons: List[str] = []
    qu_cfg = cfg.qu
    if qu_cfg.escalate_on_low_confidence and confidence < qu_cfg.qu_confidence_threshold:
        reasons.append("low_confidence")
    if len(content_tokens) >= qu_cfg.escalate_min_tokens_for_nl and (not brand_hit_found or "analog" in attrs.modifiers):
        reasons.append("nl_query")
    if is_homonym and not is_bare_noun:
        reasons.append("homonym")
    # Поиск аналога всегда эскалируется к LLM для подбора альтернатив
    if "analog" in attrs.modifiers:
        reasons.append("analog_intent")
        confidence = min(confidence, 0.55)

    return QuResult(
        intent=intent,
        type_probs=type_probs,
        predicted_types=tuple(predicted_types),
        entity_hits=tuple(assigned_hits),
        candidate_entities=candidates,
        attrs=attrs,
        rewrites=tuple(rewrites[:3]),
        clarification_chips=tuple(chips[:4]),
        confidence=round(min(1.0, confidence), 4),
        is_bare_noun=is_bare_noun,
        is_homonym=is_homonym,
        is_out_of_catalog_hint=is_ooc_hint,
        escalate_to_llm=bool(reasons),
        escalation_reasons=tuple(reasons),
        source="rules",
    )