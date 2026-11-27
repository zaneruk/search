import pytest

from services.api.core.index_state import get_index_state
from services.api.nlp import analyze_query_rules
from services.api.retrieval import run_retrieval


def test_retrieval_on_current_index():
    idx = get_index_state()
    if not idx.is_loaded:
        pytest.skip("data/artifacts/current not present")

    # Проверяем L1+L2+L3+L5 быстро (без загрузки 2GB BAAI/bge-m3 в unit-тесте)
    for query in ("astra linux", "крипто про csp 5.0", "сканер-вс", "кибер бэкап", "сервер", "мдаемон"):
        analysis = analyze_query_rules(query, index_state=idx)
        res = run_retrieval(analysis, idx, disable_dense=True)
        assert len(res.candidates) > 0, f"empty candidates for {query}"

    # "мдаемон" должен отдавать EXTERNAL_PRODUCT в nav_hits и баннер
    md = run_retrieval(analyze_query_rules("мдаемон", index_state=idx), idx, disable_dense=True)
    assert any(h.kind == "EXTERNAL_PRODUCT" for h in md.nav_hits)
    assert md.out_of_catalog_banner

    # "кибер бэкап" должен находить CATEGORY/CONTENT или товары Киберпротект
    kb = run_retrieval(analyze_query_rules("кибер бэкап", index_state=idx), idx, disable_dense=True)
    assert any("кибер" in c.canonical_name.lower() or "кибер" in c.vendor.lower() for c in kb.candidates[:10])