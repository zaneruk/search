"""S9: MMR-разнообразие и сборка секций (включая мультиинтент и честные пустые блоки)."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

from services.api.app.schemas import ClientContext, ProductCard, ProductClass, RelevanceLabel, SearchSection
from services.api.core.index_state import IndexState
from services.api.core.overlay import OverlaySnapshot
from services.api.growth.price_benchmark import evaluate_price_advantage
from services.api.growth.promo import build_badges
from services.api.nlp import NlpAnalysis
from services.api.ranking.features import build_feature_contributions
from services.api.ranking.scorer import ScoredCandidate
from services.api.ranking.variants import resolve_variants

_CLASS_TITLES = {
    "hardware": "Оборудование",
    "cloud_service": "Облачные решения (IaaS / SaaS)",
    "software": "Программное обеспечение",
    "subscription": "Подписки и облачные сервисы",
    "certificate": "Сертификаты и лицензии",
    "training/webinar": "Обучение и вебинары",
    "other": "Другие решения",
}


def _to_product_class(raw: str) -> ProductClass:
    try:
        return ProductClass(raw)
    except ValueError:
        return ProductClass.other


def materialize_card(
    sc: ScoredCandidate,
    analysis: NlpAnalysis,
    ctx: ClientContext,
    index_state: IndexState,
    weights: Dict[str, float],
) -> Optional[ProductCard]:
    cand = sc.candidate
    fam = index_state.family_row(cand.family_id) or {}
    rep_oid = str(fam.get("representative_offer_id") or "")
    var = resolve_variants(cand.family_id, analysis, ctx, index_state, preferred_offer_id=rep_oid or None)
    if var is None:
        return None

    adv_badge = evaluate_price_advantage(cand.family_id, cand.product_type, var.representative, index_state)
    badges = build_badges(
        var.representative,
        is_ru_registry=cand.attrs.is_ru_registry,
        price_advantage_badge=adv_badge,
    )
    img = fam.get("image_url")
    image_url = str(img) if img is not None and str(img).lower() not in ("", "nan", "none") else None

    fill_reason = sc.fill_reason
    if sc.relevance_label in (RelevanceLabel.similar, RelevanceLabel.popular) and not fill_reason:
        fill_reason = "Близкое решение в каталоге"

    return ProductCard(
        family_id=cand.family_id,
        canonical_name=cand.canonical_name,
        vendor=cand.vendor,
        product_class=_to_product_class(cand.product_class),
        product_type=cand.product_type,
        image_url=image_url,
        representative=var.representative,
        n_variants=var.n_variants,
        variant_axes=var.variant_axes,
        price_min_minor=var.price_min_minor,
        badges=badges,
        relevance_label=sc.relevance_label,
        fill_reason=fill_reason,
        matched_fields=list(cand.matched_fields),
        feature_contributions=build_feature_contributions(sc.features, weights),
        score=sc.score,
    )


def apply_mmr(
    scored: List[ScoredCandidate],
    *,
    is_navigational: bool = False,
    mmr_lambda: float = 0.75,
    max_per_vendor_top4: int = 2,
    limit: int = 20,
) -> List[ScoredCandidate]:
    if is_navigational or len(scored) <= 4:
        return scored[:limit]

    selected: List[ScoredCandidate] = []
    remaining = list(scored)
    vendor_counts: Dict[str, int] = {}

    while remaining and len(selected) < limit:
        best_idx = 0
        best_val = -1e9
        for i, item in enumerate(remaining[:30]):
            v = item.candidate.vendor.lower()
            v_cnt = vendor_counts.get(v, 0) if v else 0
            # В top-4 ограничиваем монополию одного вендора, если есть альтернативы
            if len(selected) < 4 and v_cnt >= max_per_vendor_top4 and len(remaining) > (4 - len(selected)):
                continue
            penalty = (1.0 - mmr_lambda) * (0.25 * v_cnt)
            val = mmr_lambda * item.score - penalty
            if val > best_val:
                best_val = val
                best_idx = i
        chosen = remaining.pop(best_idx)
        selected.append(chosen)
        v_key = chosen.candidate.vendor.lower()
        if v_key:
            vendor_counts[v_key] = vendor_counts.get(v_key, 0) + 1

    return selected


def compose_sections(
    cards: List[ProductCard],
    analysis: NlpAnalysis,
    *,
    out_of_catalog_banner: Optional[str] = None,
    max_items_per_section: int = 8,
) -> List[SearchSection]:
    if not cards:
        return []

    qu = analysis.qu
    # Мультиинтентная секционная выдача при омонимах / голых существительных (напр. «сервер»)
    significant_classes = [cls for cls, p in sorted(qu.type_probs.items(), key=lambda kv: -kv[1]) if p >= 0.10]
    if (qu.is_bare_noun or qu.is_homonym) and len(significant_classes) >= 2:
        sections: List[SearchSection] = []
        used: Set[str] = set()
        for idx, cls_code in enumerate(significant_classes[:3]):
            cls_items = [c for c in cards if c.product_class.value == cls_code and c.family_id not in used][:max_items_per_section]
            for it in cls_items:
                used.add(it.family_id)
            empty_reason = None
            if not cls_items and idx == 0:
                empty_reason = (
                    f"Позиции категории «{_CLASS_TITLES.get(cls_code, cls_code)}» в текущем каталоге "
                    "отсутствуют или недоступны — ниже показаны альтернативные решения."
                )
            if cls_items or empty_reason:
                sections.append(
                    SearchSection(
                        section_id=f"sec_{cls_code.replace('/', '_')}",
                        title=_CLASS_TITLES.get(cls_code, cls_code),
                        intent_class=_to_product_class(cls_code),
                        empty_reason=empty_reason,
                        items=cls_items,
                    )
                )
        leftover = [c for c in cards if c.family_id not in used][:max_items_per_section]
        if leftover:
            sections.append(SearchSection(section_id="sec_other", title="Дополнительные решения", items=leftover))
        if sections:
            return sections

    # При запросе российских аналогов или EXTERNAL_PRODUCT выделяем подтверждённые позиции реестра РФ
    if out_of_catalog_banner or "ru_registry" in qu.attrs.modifiers or "analog" in qu.attrs.modifiers:
        ru_items = [c for c in cards if any(b.type == "ru_registry" for b in c.badges)][:max_items_per_section]
        other_items = [c for c in cards if c not in ru_items][:max_items_per_section]
        if ru_items and other_items:
            return [
                SearchSection(section_id="sec_ru_analogs", title="Российские аналоги и реестр РФ", items=ru_items),
                SearchSection(section_id="sec_main", title="Доступные решения в каталоге", items=other_items),
            ]

    return [SearchSection(section_id="sec_main", title="Результаты поиска", items=cards[:max_items_per_section * 2])]