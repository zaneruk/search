"""
Симулятор B2B-сессий пользователей (п. 4.3, CONTRACTS.md A.1, A.2).
Генерирует реалистичную воронку (поиск -> подсказки -> клики -> переформулировки -> корзина/КП -> заказ / abandon)
и проблемные кластеры (R1, R2, R3, R8) для последующей работы майнера синонимов и агента.
Все записи строго помечены source='simulated'.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pandas as pd

from services.api.app.routers.admin_deps import (
    get_artifacts_dir,
    get_current_config_version,
    get_db,
)
from services.worker.agent.tools import norm_str, probe_search

PERSONAS = [
    {"persona_id": "accountant_smb", "segment": "smb", "weight": 0.35},
    {"persona_id": "sysadmin_gov", "segment": "gov", "weight": 0.30},
    {"persona_id": "buyer_edu", "segment": "edu", "weight": 0.15},
    {"persona_id": "cto_enterprise", "segment": "enterprise", "weight": 0.20},
]

# Реалистичный пул B2B-сценариев: успешные + переформулировки (для mine_synonyms) + провалы R1/R2/R3
SCENARIO_TEMPLATES: List[Dict[str, Any]] = [
    # Успешные запросы из каталога
    {"kind": "ok", "query": "kaspersky endpoint security", "hint": "Kaspersky", "ctr": 0.85, "cart_p": 0.32, "order_p": 0.35},
    {"kind": "ok", "query": "1с бухгалтерия проф", "hint": "1С", "ctr": 0.80, "cart_p": 0.28, "order_p": 0.32},
    {"kind": "ok", "query": "astra linux рабочая станция", "hint": "ALSE", "ctr": 0.82, "cart_p": 0.30, "order_p": 0.30},
    {"kind": "ok", "query": "криптопро csp", "hint": "КриптоПро", "ctr": 0.88, "cart_p": 0.35, "order_p": 0.40},
    {"kind": "ok", "query": "кибер бэкап", "hint": "Кибер Бэкап", "ctr": 0.78, "cart_p": 0.25, "order_p": 0.30},
    {"kind": "ok", "query": "microsoft office 2024", "hint": "Microsoft Office", "ctr": 0.84, "cart_p": 0.30, "order_p": 0.35},
    {"kind": "ok", "query": "сканер штрих-кода", "hint": "Mertech", "ctr": 0.75, "cart_p": 0.22, "order_p": 0.28},
    {"kind": "ok", "query": "ибп для сервера", "hint": "Powercom", "ctr": 0.72, "cart_p": 0.24, "order_p": 0.25},

    # Цепочки переформулировок (q1 без успеха -> q2 с кликом) для mine_synonyms и R2
    {"kind": "refine_success", "q1": "антивирь для буха", "q2": "kaspersky small office", "hint": "Kaspersky"},
    {"kind": "refine_success", "q1": "прога для удаленки", "q2": "anydesk лицензия", "hint": "AnyDesk"},
    {"kind": "refine_success", "q1": "замена акрониса", "q2": "кибер бэкап", "hint": "Кибер Бэкап"},
    {"kind": "refine_success", "q1": "ключ эцп для налоговой", "q2": "криптопро csp", "hint": "КриптоПро"},

    # Чистые провалы R2 (отсутствующий сленг/аббревиатура — агент сможет предложить ADD_SYNONYM и поднять Hit@4!)
    {"kind": "fail_r2", "query": "скзи для сдачи отчетности", "expected_hint": "КриптоПро"},
    {"kind": "fail_r2", "query": "вкс софтлайн облако", "expected_hint": "МТС Линк"},
    {"kind": "fail_r2", "query": "бесперебойник в стойку", "expected_hint": "ИБП"},

    # Провалы R1 (опечатка / раскладка)
    {"kind": "fail_r1", "query": "rfgthcrbq антивирус", "expected_hint": "Kaspersky"},
    {"kind": "fail_r1", "query": "фныедеск", "expected_hint": "AnyDesk"},

    # Внешние продукты R3 (CATALOG_GAP)
    {"kind": "gap_r3", "query": "figma корпоративная лицензия"},
    {"kind": "gap_r3", "query": "notion team подписка"},
    {"kind": "gap_r3", "query": "мдаемон почтовый сервер"},
]


def _make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def _pick_sample_family_by_hint(fdf: pd.DataFrame, hint: str) -> Optional[Dict[str, Any]]:
    if len(fdf) == 0:
        return None
    sub = fdf[fdf["canonical_name"].fillna("").str.contains(hint, case=False, regex=False)]
    if len(sub) == 0:
        sub = fdf[fdf["vendor"].fillna("").str.contains(hint, case=False, regex=False)]
    if len(sub) == 0:
        return None
    row = sub.sort_values(["available_any", "pop_prior"], ascending=[False, False]).iloc[0]
    return {
        "family_id": str(row["family_id"]),
        "canonical_name": str(row["canonical_name"]),
        "price_min_minor": int(row["price_min_minor"]) if pd.notna(row["price_min_minor"]) else None,
        "available_any": bool(row["available_any"]),
    }


def _try_api_search(client: Optional[httpx.Client], api_url: str, query: str, session_id: str, segment: str) -> Optional[Dict[str, Any]]:
    if client is None:
        return None
    try:
        r = client.post(
            f"{api_url}/v1/search",
            json={
                "query": query,
                "session_id": session_id,
                "context": {"segment": segment},
                "options": {"mode": "smart", "llm_enabled": False, "include_trace": False},
            },
            timeout=2.0,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def run_simulator(
    n_sessions: int = 80,
    seed: int = 42,
    via_api: bool = False,
) -> Dict[str, Any]:
    """
    Генерирует n_sessions B2B-сессий в SQLite с source='simulated'.
    Также создаёт несколько эталонных legacy-трасс для наглядного сравнения Legacy vs Smart в KPI.
    """
    rng = random.Random(seed + int(datetime.now().timestamp()) % 10000)
    art_dir = get_artifacts_dir()
    fam_path = (art_dir / "families.parquet") if art_dir else Path("data/interim/families.parquet")
    fdf = pd.read_parquet(fam_path) if fam_path.exists() else pd.DataFrame()

    api_url = os.getenv("INTERNAL_API_URL", "http://127.0.0.1:8000").rstrip("/")
    http_client = httpx.Client() if via_api else None

    now_dt = datetime.now(timezone.utc)
    traces_written = 0
    events_written = 0

    def add_event(conn, ts_str: str, ev_type: str, session_id: str, trace_id: Optional[str], cfg_ver: int, payload: Dict[str, Any]) -> None:
        nonlocal events_written
        conn.execute(
            """
            INSERT INTO events (event_id, ts, event_type, session_id, trace_id, source, config_version, payload_json)
            VALUES (?, ?, ?, ?, ?, 'simulated', ?, ?)
            """,
            (_make_id("ev"), ts_str, ev_type, session_id, trace_id, cfg_ver, json.dumps(payload, ensure_ascii=False)),
        )
        events_written += 1

    def add_trace(
        conn,
        trace_id: str,
        ts_str: str,
        session_id: str,
        query_raw: str,
        mode: str,
        cfg_ver: int,
        hard_null: bool,
        soft_null: bool,
        fill_rung: int,
        latency_ms: float,
        resp_summary: Dict[str, Any],
    ) -> None:
        nonlocal traces_written
        conn.execute(
            """
            INSERT OR IGNORE INTO search_traces (
                trace_id, ts, session_id, source, query_raw, query_norm,
                mode, degradation_level, config_version, hard_null, soft_null,
                fill_rung, latency_ms, response_json
            ) VALUES (?, ?, ?, 'simulated', ?, ?, ?, 'L0', ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                ts_str,
                session_id,
                query_raw,
                norm_str(query_raw),
                mode,
                cfg_ver,
                1 if hard_null else 0,
                1 if soft_null else 0,
                fill_rung,
                round(latency_ms, 2),
                json.dumps(resp_summary, ensure_ascii=False),
            ),
        )
        traces_written += 1

    try:
        with get_db() as conn:
            cfg_ver = get_current_config_version(conn)

            for s_idx in range(n_sessions):
                persona = rng.choices(PERSONAS, weights=[p["weight"] for p in PERSONAS], k=1)[0]
                session_id = _make_id("ss_sim")
                base_ts = now_dt - timedelta(minutes=rng.randint(1, 360))
                sc = rng.choice(SCENARIO_TEMPLATES)
                kind = sc["kind"]

                if kind == "ok":
                    q = sc["query"]
                    tid = _make_id("tr")
                    ts1 = base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                    api_resp = _try_api_search(http_client, api_url, q, session_id, persona["segment"])
                    if api_resp:
                        hard_null = bool(api_resp.get("hard_null"))
                        soft_null = bool(api_resp.get("soft_null"))
                        fill_rung = int(api_resp.get("fill_rung", 0))
                    else:
                        hard_null, soft_null, fill_rung = False, False, 0

                    fam = _pick_sample_family_by_hint(fdf, sc["hint"])
                    add_trace(conn, tid, ts1, session_id, q, "smart", cfg_ver, hard_null, soft_null, fill_rung, rng.uniform(35.0, 145.0), {"top1": fam})
                    add_event(conn, ts1, "search_issued", session_id, tid, cfg_ver, {"query": q, "segment": persona["segment"]})

                    # Эмулируем также Legacy-запрос в 25% сессий, чтобы в KPI было честное сравнение Legacy vs Smart
                    if rng.random() < 0.25:
                        ltid = _make_id("tr")
                        leg_fail = rng.random() < 0.38
                        add_trace(conn, ltid, ts1, session_id, q, "legacy", cfg_ver, leg_fail, leg_fail, 0, rng.uniform(15.0, 45.0), {})

                    if fam and rng.random() < sc["ctr"]:
                        ts2 = (base_ts + timedelta(seconds=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        add_event(conn, ts2, "result_click", session_id, tid, cfg_ver, {"family_id": fam["family_id"], "rank": 1})
                        if rng.random() < sc["cart_p"]:
                            ts3 = (base_ts + timedelta(seconds=18)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            ev_conv = "add_to_cart" if fam["price_min_minor"] else "quote_request"
                            add_event(conn, ts3, ev_conv, session_id, tid, cfg_ver, {"family_id": fam["family_id"]})
                            if rng.random() < sc["order_p"]:
                                ts4 = (base_ts + timedelta(seconds=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
                                add_event(
                                    conn,
                                    ts4,
                                    "order",
                                    session_id,
                                    tid,
                                    cfg_ver,
                                    {"family_id": fam["family_id"], "amount_minor": fam["price_min_minor"] or 1500000},
                                )
                    else:
                        ts2 = (base_ts + timedelta(seconds=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        add_event(conn, ts2, "abandon", session_id, tid, cfg_ver, {"query": q})

                elif kind == "refine_success":
                    tid1 = _make_id("tr")
                    ts1 = base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                    add_trace(conn, tid1, ts1, session_id, sc["q1"], "smart", cfg_ver, False, True, 4, rng.uniform(70.0, 180.0), {})
                    add_event(conn, ts1, "search_issued", session_id, tid1, cfg_ver, {"query": sc["q1"]})

                    ts2 = (base_ts + timedelta(seconds=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    add_event(conn, ts2, "refine", session_id, tid1, cfg_ver, {"from_query": sc["q1"], "to_query": sc["q2"]})

                    tid2 = _make_id("tr")
                    ts3 = (base_ts + timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fam = _pick_sample_family_by_hint(fdf, sc["hint"])
                    add_trace(conn, tid2, ts3, session_id, sc["q2"], "smart", cfg_ver, False, False, 0, rng.uniform(45.0, 130.0), {"top1": fam})
                    add_event(conn, ts3, "search_issued", session_id, tid2, cfg_ver, {"query": sc["q2"]})
                    if fam:
                        ts4 = (base_ts + timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        add_event(conn, ts4, "result_click", session_id, tid2, cfg_ver, {"family_id": fam["family_id"], "rank": 1})

                elif kind in ("fail_r1", "fail_r2"):
                    q = sc["query"]
                    tid = _make_id("tr")
                    ts1 = base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                    pr = probe_search(q, None, conn)
                    hard_n = bool(pr.get("hard_null", True))
                    add_trace(conn, tid, ts1, session_id, q, "smart", cfg_ver, hard_n, True, 5 if not hard_n else 6, rng.uniform(80.0, 210.0), {})
                    add_event(conn, ts1, "search_issued", session_id, tid, cfg_ver, {"query": q})
                    ts2 = (base_ts + timedelta(seconds=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if rng.random() < 0.65:
                        add_event(conn, ts2, "abandon", session_id, tid, cfg_ver, {"query": q})
                    else:
                        add_event(conn, ts2, "feedback", session_id, tid, cfg_ver, {"query": q, "rating": -1, "liked": False})

                elif kind == "gap_r3":
                    q = sc["query"]
                    tid = _make_id("tr")
                    ts1 = base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                    add_trace(conn, tid, ts1, session_id, q, "smart", cfg_ver, True, True, 6, rng.uniform(55.0, 140.0), {})
                    add_event(conn, ts1, "search_issued", session_id, tid, cfg_ver, {"query": q})
                    ts2 = (base_ts + timedelta(seconds=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if rng.random() < 0.45:
                        add_event(conn, ts2, "catalog_gap_lead", session_id, tid, cfg_ver, {"query": q, "segment": persona["segment"]})
                    else:
                        add_event(conn, ts2, "abandon", session_id, tid, cfg_ver, {"query": q})

            conn.commit()
    finally:
        if http_client is not None:
            http_client.close()

    return {
        "status": "ok",
        "sessions_generated": n_sessions,
        "traces_written": traces_written,
        "events_written": events_written,
        "source": "simulated",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Симулятор демо-сессий (source='simulated')")
    parser.add_argument("--sessions", type=int, default=80)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--via-api", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_simulator(n_sessions=args.sessions, seed=args.seed, via_api=args.via_api), ensure_ascii=False, indent=2))