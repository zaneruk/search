import pytest

from services.api.app.schemas import ClientContext, PriceState, RelevanceLabel
from services.api.core.index_state import get_index_state
from services.api.core.overlay import OverlaySnapshot
from services.api.growth import build_cross_sell
from services.api.nlp import analyze_query_rules
from services.api.ranking import (
    apply_guarantee_ladder,
    apply_mmr,
    compose_sections,
    materialize_card,
    score_candidates,
)
from services.api.retrieval import run_retrieval


def test_ranking_growth_and_invariants_on_real_index():
    idx = get_index_state()
    if not idx.is_loaded:
        pytest.skip("data/artifacts/current not present")

    overlay = OverlaySnapshot.empty()
    weights = dict(overlay.ranking_weights)
    ctx = ClientContext()

    for query in ("", "!@#$%^&*", "anydesk", "astra linux", "крипто про csp 5.0", "сервер", "мдаемон"):
        analysis = analyze_query_rules(query, ctx=ctx, index_state=idx, overlay=overlay)
        ret = run_retrieval(analysis, idx, ctx=ctx, disable_dense=True)
        scored = score_candidates(ret.candidates, analysis, ctx, idx, overlay=overlay)
        mmr = apply_mmr(scored, is_navigational=(analysis.qu.intent == "exact_entity"), limit=20)
        primary = [c for c in (materialize_card(s, analysis, ctx, idx, weights) for s in mmr) if c is not None]
        ladder = apply_guarantee_ladder(
            primary,
            [s for s in scored if s not in mmr],
            analysis,
            ctx,
            idx,
            weights,
            hard_null=ret.hard_null,
        )
        sections = compose_sections(ladder.cards, analysis, out_of_catalog_banner=ret.out_of_catalog_banner)
        shown = [item for sec in sections for item in sec.items]
        distinct_fids = {item.family_id for item in shown}

        # I1: >= 4 distinct families всегда (включая "" и "!@#$%^&*")
        assert len(distinct_fids) >= 4, f"I1 failed for {query!r}: {len(distinct_fids)}"

        for card in shown:
            rep = card.representative
            # I2: никогда price == 0; on_request <=> price_minor is None
            assert rep.price_minor != 0
            assert (rep.price_state == PriceState.on_request) == (rep.price_minor is None)

            # I3: promo iff oldprice > price > 0 and discount_pct <= 70
            has_promo = any(b.type == "promo" for b in card.badges)
            expected_promo = (
                rep.oldprice_minor is not None
                and rep.price_minor is not None
                and rep.oldprice_minor > rep.price_minor > 0
                and ((rep.oldprice_minor - rep.price_minor) / rep.oldprice_minor * 100.0) <= 70.0
            )
            assert has_promo == expected_promo, f"I3 mismatch on {card.family_id}"

            # I5: similar / popular обязаны иметь непустой fill_reason
            if card.relevance_label in (RelevanceLabel.similar, RelevanceLabel.popular):
                assert card.fill_reason, f"I5 failed on {card.family_id}"

        # I6: допродажи без unavailable, on_request, купленных и дублей
        purchased_ctx = ClientContext(purchased_family_ids=[shown[0].family_id])
        cs = build_cross_sell([c.family_id for c in shown[:3]], distinct_fids, purchased_ctx, idx)
        for item in cs:
            assert item.available is True
            assert item.price_min_minor is not None and item.price_min_minor > 0
            assert item.family_id not in distinct_fids
            assert item.family_id not in purchased_ctx.purchased_family_ids