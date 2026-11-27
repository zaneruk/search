import asyncio
from types import SimpleNamespace

import pandas as pd

from services.api.app.schemas import ClientContext, DegradationLevel, QuLlmOutput
from services.api.core.index_state import IndexState
from services.api.core.overlay import OverlaySnapshot
from services.api.llm.gateway import LLMResult
from services.api.nlp import (
    analyze_query,
    analyze_query_rules,
    convert_en_to_ru,
    same_sound,
)


def _mock_index() -> IndexState:
    entities = pd.DataFrame([
        {
            "entity_id": "e_brand_kaspersky",
            "kind": "BRAND",
            "canonical_name": "Kaspersky",
            "aliases_json": '["касперский", "kaspersky"]',
            "status": "seed",
            "target_family_ids_json": "[]",
        },
        {
            "entity_id": "e_brand_cyber",
            "kind": "BRAND",
            "canonical_name": "Киберпротект",
            "aliases_json": '["cyber backup", "кибер бэкап", "cyberprotect"]',
            "status": "seed",
            "target_family_ids_json": '["f_100"]',
        },
        {
            "entity_id": "e_ext_mdaemon",
            "kind": "EXTERNAL_PRODUCT",
            "canonical_name": "MDaemon",
            "aliases_json": '["мдаемон", "mdaemon"]',
            "status": "seed",
            "target_family_ids_json": "[]",
        },
    ])
    return IndexState(is_loaded=True, index_version="idx_test", entities=entities)


def test_normalize_homoglyphs_hyphen_and_noise():
    res = analyze_query_rules("Купить  кaсперский — цена 5,0!", index_state=IndexState.unloaded())
    assert "купить" in res.normalized.noise_tokens
    assert "цена" in res.normalized.noise_tokens
    assert "касперский" in res.normalized.tokens
    assert "5.0" in res.normalized.version_tokens

    only_noise = analyze_query_rules("купить")
    assert only_noise.normalized.query_norm == "купить"


def test_layout_and_translit():
    assert convert_en_to_ru("ghbdtn") == "привет"
    assert same_sound("кибер", "cyber")
    assert same_sound("бэкап", "backup")
    assert same_sound("мдаемон", "mdaemon")

    # Валидный бренд не должен ломаться раскладкой
    res = analyze_query_rules("kaspersky", index_state=_mock_index())
    assert res.hypotheses.applied_query is None
    assert res.hypotheses.protected_exact


def test_bare_noun_vs_for_server():
    bare = analyze_query_rules("сервер", index_state=IndexState.unloaded())
    assert bare.qu.is_bare_noun
    assert bare.qu.type_probs.get("hardware", 0.0) + bare.qu.type_probs.get("cloud_service", 0.0) > bare.qu.type_probs.get("software", 0.0)

    rel = analyze_query_rules("антивирус для сервера", index_state=IndexState.unloaded())
    assert not rel.qu.is_bare_noun
    assert rel.qu.attrs.relation == "для"
    assert rel.qu.type_probs.get("software", 0.0) >= 0.5


def test_attrs_and_nl_escalation_with_closed_set_filter():
    nl = analyze_query_rules("чем защитить корпоративные серверы на 12 месяцев на 25 пользователей", index_state=_mock_index())
    assert nl.qu.attrs.term_months == 12
    assert nl.qu.attrs.qty_users == 25
    assert nl.qu.attrs.segment == "enterprise"
    assert nl.qu.escalate_to_llm

    class FakeGateway:
        async def complete_json(self, **kwargs):
            post_val = kwargs["post_validate"]
            raw_out = QuLlmOutput(
                intent="find_solution",
                type_probs={"software": 0.9, "cloud_service": 0.1},
                entity_ids=["e_brand_kaspersky", "e_hallucinated_999"],
                rewrites=["антивирус для серверов"],
                clarification_chips=[],
                confidence=0.92,
            )
            cleaned = post_val(raw_out) if post_val else raw_out
            return LLMResult(ok=True, parsed=cleaned, from_cache=False)

    fake_deg = SimpleNamespace(allow_llm=lambda requested=True: True, level=lambda: DegradationLevel.L0)
    out = asyncio.run(
        analyze_query(
            "чем защитить серверы от шифровальщиков",
            ctx=ClientContext(),
            index_state=_mock_index(),
            overlay=OverlaySnapshot.empty(),
            gateway=FakeGateway(),
            degradation=fake_deg,
        )
    )
    assert out.qu.source == "llm"
    assert all(h.ref.entity_id != "e_hallucinated_999" for h in out.qu.entity_hits)