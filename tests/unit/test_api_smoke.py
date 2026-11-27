import json
import os
from fastapi.testclient import TestClient
from services.api.app.main import app


def test_public_and_internal_endpoints():
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert "X-Trace-Id" in r.headers

        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["config_version"] >= 1

        # Typeahead (< 60ms)
        sug = client.get("/v1/suggest", params={"q": "kasp", "segment": "smb"})
        assert sug.status_code == 200
        sdata = sug.json()
        assert len(sdata["completions"]) > 0
        assert len(sdata["products"]) > 0

        # Legacy vs Smart для «сервер»
        leg = client.post("/v1/search", json={"query": "сервер", "options": {"mode": "legacy", "disable_dense": True}})
        assert leg.status_code == 200
        assert leg.json()["mode"] == "legacy"

        smt = client.post("/v1/search", json={"query": "сервер", "options": {"mode": "smart", "llm_enabled": False, "disable_dense": True}})
        assert smt.status_code == 200
        sbody = smt.json()
        assert sbody["mode"] == "smart"
        assert len({it["family_id"] for sec in sbody["sections"] for it in sec["items"]}) >= 4
        assert len(sbody["trace"]) == 13  # S0..S12

        # SSE стрим
        with client.stream("POST", "/v1/search/stream", json={"query": "крипто про csp 5.0", "options": {"llm_enabled": False, "disable_dense": True}}) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
            assert "event: span" in raw
            assert "event: final" in raw

        # Events
        ev = client.post("/v1/events", json={"event_type": "result_click", "session_id": "ss_test", "payload": {"email": "a@b.ru"}})
        assert ev.status_code == 202
        assert "event_id" in ev.json()

        # Internal probe (без токена -> 401, с токеном -> 200)
        unauth = client.post("/internal/probe", json={"query": "мдаемон"})
        assert unauth.status_code == 401

        tok = os.getenv("INTERNAL_TOKEN", "change-me-internal")
        prb = client.post(
            "/internal/probe",
            headers={"X-Internal-Token": tok},
            json={"query": "мдаемон", "temp_patch": {"kind": "synonym", "key": "мдаемон", "value": {"synonyms": ["почта"]}}},
        )
        assert prb.status_code == 200
        assert len({it["family_id"] for sec in prb.json()["sections"] for it in sec["items"]}) >= 4