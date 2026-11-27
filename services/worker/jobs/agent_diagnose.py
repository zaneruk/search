"""
Агент диагностики провальных поисков (п. 4.4, CONTRACTS.md A.8, B.10):
1. Отбирает hard_null, soft_null, abandon, дизлайки и проблемные цепочки.
2. Группирует в кластеры по отсортированному набору лемм.
3. Собирает evidence (entity lookup, catalog_stats, mine_synonyms, catalog_gap_lead).
4. Классифицирует по R1..R10 (правила + Yandex AI Studio deepseek-v4.1-flash со строгой проверкой патча).
5. Прогоняет обязательный dry-run через probe_search (до и после патча).
6. Создаёт/обновляет тикеты в SQLite.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from services.api.app.routers.admin_deps import get_db
from services.api.llm.gateway import call_llm_structured, get_llm_credentials
from services.worker.agent.tickets import estimate_monthly_rub, upsert_ticket
from services.worker.agent.tools import (
    catalog_stats,
    lookup_entity,
    norm_str,
    probe_search,
    query_failures,
    tokenize_lemmas,
)
from services.worker.jobs.mine_synonyms import mine_synonym_candidates

try:
    from services.api.app.schemas.llm import AgentDiagnoseOutput
    from services.api.app.schemas.admin import OverlayRulePayload
    from services.api.app.schemas.common import RootCause, TicketType
except ImportError:
    from enum import Enum

    class RootCause(str, Enum):
        R1 = "R1"
        R2 = "R2"
        R3 = "R3"
        R4 = "R4"
        R5 = "R5"
        R6 = "R6"
        R7 = "R7"
        R8 = "R8"
        R9 = "R9"
        R10 = "R10"

    class TicketType(str, Enum):
        ADD_SYNONYM = "ADD_SYNONYM"
        ADD_ALIAS = "ADD_ALIAS"
        CATALOG_GAP = "CATALOG_GAP"
        FIX_CARD = "FIX_CARD"
        RANKING_RULE = "RANKING_RULE"
        AVAILABILITY_ALERT = "AVAILABILITY_ALERT"
        INTENT_DISAMBIGUATION = "INTENT_DISAMBIGUATION"

    class OverlayRulePayload(BaseModel):
        kind: str
        key: str
        value: Dict[str, Any]

    class AgentDiagnoseOutput(BaseModel):
        root_cause: RootCause
        ticket_type: TicketType
        target_role: str
        confidence: float = Field(ge=0.0, le=1.0)
        summary: str
        proposed_patch: Optional[OverlayRulePayload] = None


_EN_TO_RU_LAYOUT = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.",
    "йцукенгшщзхъфывапролджэячсмитьбю",
)

_KNOWN_EN_TOKENS = {
    "microsoft", "office", "excel", "word", "windows", "server", "endpoint",
    "security", "small", "standard", "pro", "enterprise", "linux", "astra",
    "kaspersky", "anydesk", "backup", "cyber", "cloud", "max", "patrol",
    "team", "sql", "oracle", "adobe", "figma", "notion", "mdaemon",
    "webinar", "csp", "vpn", "sip", "ups", "nas", "ssd", "hdd", "pdf",
    "jira", "slack", "confluence", "dr.web", "eset", "autocad", "nanocad",
}

_DOMAIN_SLANG_HINTS: List[Tuple[str, str, str]] = [
    ("скзи", "КриптоПро CSP", "R2"),
    ("эцп", "КриптоПро CSP", "R2"),
    ("вкс", "МТС Линк", "R2"),
    ("бесперебойник", "ИБП", "R2"),
    ("акронис", "Кибер Бэкап", "R2"),
    ("антивирь", "Kaspersky", "R2"),
    ("удаленк", "AnyDesk", "R2"),
    ("энидеск", "AnyDesk", "R1"),
    ("фныедеск", "AnyDesk", "R1"),
    ("каспер", "Kaspersky", "R1"),
]


def _decode_layout(text: str) -> Optional[str]:
    q = norm_str(text)
    tokens = q.split()
    if any(t in _KNOWN_EN_TOKENS for t in tokens):
        changed = False
        out_toks = []
        for t in tokens:
            if re.fullmatch(r"[a-z\[\];',.]{5,}", t) and t not in _KNOWN_EN_TOKENS:
                out_toks.append(t.translate(_EN_TO_RU_LAYOUT))
                changed = True
            else:
                out_toks.append(t)
        return " ".join(out_toks) if changed else None

    if re.search(r"[a-z]", q) and not re.search(r"[а-я]", q):
        cand = q.translate(_EN_TO_RU_LAYOUT)
        if cand != q:
            return cand

    changed = False
    out_toks = []
    for t in tokens:
        if re.fullmatch(r"[a-z\[\];',.]{4,}", t):
            out_toks.append(t.translate(_EN_TO_RU_LAYOUT))
            changed = True
        else:
            out_toks.append(t)
    return " ".join(out_toks) if changed else None


def _is_nonsense(q: str) -> bool:
    s = re.sub(r"\s+", "", norm_str(q))
    if len(s) < 2:
        return True
    if len(s) >= 6 and len(set(s)) <= 2:
        return True
    if not re.search(r"[a-zа-я0-9]", s):
        return True
    return False


def _rule_based_diagnose(
    cluster: Dict[str, Any],
    syn_map: Dict[str, Dict[str, Any]],
    entities: List[Dict[str, Any]],
    cat_stat: Dict[str, Any],
    before_res: Dict[str, Any],
) -> Dict[str, Any]:
    q = cluster["representative_query"]
    q_norm = norm_str(q)

    if _is_nonsense(q_norm):
        return {
            "root_cause": "R10",
            "ticket_type": "FIX_CARD",
            "target_role": "content",
            "confidence": 0.9,
            "summary": f"Шумовой / бессмысленный запрос «{q}» — патч не требуется.",
            "proposed_patch": None,
        }

    ext_hits = [e for e in entities if e["kind"] == "EXTERNAL_PRODUCT"]
    if ext_hits or (cluster["gap_leads"] > 0 and cat_stat.get("total_families", 0) == 0):
        ext_name = ext_hits[0]["canonical_name"] if ext_hits else q
        return {
            "root_cause": "R3",
            "ticket_type": "CATALOG_GAP",
            "target_role": "product",
            "confidence": 0.94,
            "summary": f"Спрос на отсутствующий в каталоге продукт «{ext_name}» (запросов: {cluster['frequency']}, лидов КП: {cluster['gap_leads']}).",
            "proposed_patch": None,
        }

    if q_norm in syn_map:
        cand = syn_map[q_norm]
        target_q = cand["q2_norm"]
        return {
            "root_cause": "R2",
            "ticket_type": "ADD_SYNONYM",
            "target_role": "content",
            "confidence": min(0.95, 0.72 + 0.05 * cand["support"]),
            "summary": f"Пользователи переформулируют «{q_norm}» в «{target_q}» (support={cand['support']}). Рекомендуется добавить синоним.",
            "proposed_patch": {
                "kind": "synonym",
                "key": q_norm,
                "value": {"synonyms": [target_q]},
            },
        }

    for marker, target_term, rc in _DOMAIN_SLANG_HINTS:
        if marker in q_norm:
            ttype = "ADD_ALIAS" if rc == "R1" else "ADD_SYNONYM"
            return {
                "root_cause": rc,
                "ticket_type": ttype,
                "target_role": "content",
                "confidence": 0.84,
                "summary": f"Не найден сленг/алиас в запросе «{q_norm}». Рекомендуется связать с «{target_term}».",
                "proposed_patch": {
                    "kind": "synonym" if rc == "R2" else "alias",
                    "key": q_norm,
                    "value": {"synonyms": [target_term]},
                },
            }

    decoded = _decode_layout(q_norm)
    if decoded and (before_res.get("soft_null") or float(before_res.get("hit4_score", 1.0)) < 0.8):
        target_hint = "Kaspersky" if "антивир" in decoded or "rfgt" in q_norm or "rfgth" in q_norm else decoded
        return {
            "root_cause": "R1",
            "ticket_type": "ADD_ALIAS",
            "target_role": "content",
            "confidence": 0.88,
            "summary": f"Ошибочная раскладка / опечатка в «{q_norm}» (предположительно «{target_hint}»).",
            "proposed_patch": {
                "kind": "alias",
                "key": q_norm,
                "value": {"synonyms": [target_hint, decoded]},
            },
        }

    if cat_stat.get("total_families", 0) > 0 and cat_stat.get("available_families", 0) == 0:
        return {
            "root_cause": "R8",
            "ticket_type": "AVAILABILITY_ALERT",
            "target_role": "product",
            "confidence": 0.82,
            "summary": f"По запросу «{q_norm}» найдены семейства ({cat_stat['total_families']}), но все они недоступны (available=false).",
            "proposed_patch": None,
        }

    if cat_stat.get("total_families", 0) > 0 and float(cat_stat.get("avg_quality_score", 1.0)) < 0.5:
        return {
            "root_cause": "R5",
            "ticket_type": "FIX_CARD",
            "target_role": "content",
            "confidence": 0.75,
            "summary": f"Низкое качество карточек по кластеру «{q_norm}» (avg_quality_score={cat_stat.get('avg_quality_score')}).",
            "proposed_patch": None,
        }

    if not before_res.get("hard_null") and float(before_res.get("hit4_score", 0.0)) >= 0.5:
        return {
            "root_cause": "R6",
            "ticket_type": "RANKING_RULE",
            "target_role": "ecom",
            "confidence": 0.72,
            "summary": f"По запросу «{q_norm}» товары есть в выдаче, но фиксируются уходы без клика ({cluster['abandons']}). Проверить ранжирование/варианты.",
            "proposed_patch": None,
        }

    if entities:
        top_ent = entities[0]
        return {
            "root_cause": "R2",
            "ticket_type": "ADD_SYNONYM",
            "target_role": "content",
            "confidence": 0.70,
            "summary": f"Запрос «{q_norm}» уходит в null/abandon; ближайшая сущность «{top_ent['canonical_name']}».",
            "proposed_patch": {
                "kind": "synonym",
                "key": q_norm,
                "value": {"synonyms": [top_ent["canonical_name"]]},
            },
        }

    return {
        "root_cause": "R4",
        "ticket_type": "CATALOG_GAP",
        "target_role": "product",
        "confidence": 0.65,
        "summary": f"По запросу «{q_norm}» нет точного совпадения в каталоге ({cluster['frequency']} событий).",
        "proposed_patch": None,
    }


def _call_llm_diagnose(
    conn: sqlite3.Connection,
    cluster: Dict[str, Any],
    evidence: List[str],
    entities: List[Dict[str, Any]],
    cat_stat: Dict[str, Any],
    before_res: Dict[str, Any],
    rule_diag: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    prompt_path = Path("prompts/agent_diagnose/v1.md")
    system_prompt = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.exists()
        else "Classify search failure cluster into root_cause R1..R10 and ticket_type. Output strict JSON."
    )

    user_payload = (
        "<user_input>\n"
        + json.dumps(
            {
                "cluster_key": cluster["cluster_key"],
                "queries": cluster["queries"][:5],
                "frequency": cluster["frequency"],
                "hard_nulls": cluster["hard_nulls"],
                "soft_nulls": cluster["soft_nulls"],
                "abandons": cluster["abandons"],
                "dislikes": cluster["dislikes"],
                "gap_leads": cluster["gap_leads"],
                "evidence": evidence[:5],
                "entity_candidates": [
                    {"kind": e["kind"], "name": e["canonical_name"], "score": e["match_score"]}
                    for e in entities[:4]
                ],
                "catalog_stats": cat_stat,
                "current_top4": [x.get("canonical_name") for x in (before_res.get("top4") or [])[:4]],
                "current_hit4_proxy": before_res.get("hit4_score"),
                "rule_preliminary_hint": {
                    "root_cause": rule_diag["root_cause"],
                    "proposed_patch": rule_diag.get("proposed_patch"),
                },
            },
            ensure_ascii=False,
        )
        + "\n</user_input>"
    )

    parsed, _ = call_llm_structured(
        role="agent",
        purpose="agent_diagnose",
        system_prompt=system_prompt,
        user_payload=user_payload,
        schema_cls=AgentDiagnoseOutput,
        conn=conn,
    )
    if parsed is not None:
        q_norm = norm_str(cluster["representative_query"])
        patch = parsed.get("proposed_patch")
        if isinstance(patch, dict):
            kind = patch.get("kind")
            val = patch.get("value")
            if kind not in ("synonym", "alias", "pin", "demote", "boost", "catalog_patch"):
                parsed["proposed_patch"] = None
            elif kind in ("synonym", "alias"):
                syns = val.get("synonyms") if isinstance(val, dict) else None
                clean_syns = [s.strip() for s in syns if isinstance(s, str) and s.strip()] if isinstance(syns, list) else []
                if not clean_syns:
                    parsed["proposed_patch"] = rule_diag.get("proposed_patch")
                else:
                    # Всегда привязываем key к исходному запросу, чтобы патч реально сработал в overlay
                    patch["key"] = q_norm
                    patch["value"] = {"synonyms": clean_syns[:5]}
        if parsed.get("target_role") not in ("content", "product", "ecom"):
            parsed["target_role"] = "content"
    return parsed


def run_agent_diagnose(use_llm: bool = True, max_clusters: int = 15) -> Dict[str, Any]:
    """Основная точка запуска диагностического агента."""
    _, _, api_key, _ = get_llm_credentials()
    llm_ready = use_llm and bool(api_key)

    with get_db() as conn:
        raw_fail = query_failures(conn, limit=1500)
        failed_traces = raw_fail["failed_traces"]
        neg_events = raw_fail["negative_events"]

        syn_cands = mine_synonym_candidates(conn=conn, min_support=2)
        syn_map = {c["q1_norm"]: c for c in syn_cands}

        clusters: Dict[str, Dict[str, Any]] = {}
        trace_to_cluster: Dict[str, str] = {}

        def get_cluster_key(q_norm: str) -> str:
            lemmas = sorted(set(tokenize_lemmas(q_norm)))
            return " ".join(lemmas) if lemmas else q_norm

        for tr in failed_traces:
            qn = norm_str(tr["query_norm"] or tr["query_raw"])
            if not qn:
                continue
            ckey = get_cluster_key(qn)
            trace_to_cluster[str(tr["trace_id"])] = ckey
            cl = clusters.setdefault(
                ckey,
                {
                    "cluster_key": ckey,
                    "queries": [],
                    "representative_query": str(tr["query_raw"]),
                    "frequency": 0,
                    "hard_nulls": 0,
                    "soft_nulls": 0,
                    "abandons": 0,
                    "dislikes": 0,
                    "gap_leads": 0,
                },
            )
            cl["frequency"] += 1
            cl["hard_nulls"] += int(tr["hard_null"] or 0)
            cl["soft_nulls"] += int(tr["soft_null"] or 0)
            if str(tr["query_raw"]) not in cl["queries"]:
                cl["queries"].append(str(tr["query_raw"]))

        for ev in neg_events:
            tid = str(ev["trace_id"] or "")
            ckey = trace_to_cluster.get(tid)
            try:
                pl = json.loads(ev["payload_json"])
            except Exception:
                pl = {}

            if not ckey and pl.get("query"):
                qn = norm_str(pl["query"])
                if qn:
                    ckey = get_cluster_key(qn)
                    clusters.setdefault(
                        ckey,
                        {
                            "cluster_key": ckey,
                            "queries": [str(pl["query"])],
                            "representative_query": str(pl["query"]),
                            "frequency": 1,
                            "hard_nulls": 0,
                            "soft_nulls": 0,
                            "abandons": 0,
                            "dislikes": 0,
                            "gap_leads": 0,
                        },
                    )
            if not ckey or ckey not in clusters:
                continue
            et = ev["event_type"]
            if et == "abandon":
                clusters[ckey]["abandons"] += 1
            elif et == "feedback":
                clusters[ckey]["dislikes"] += 1
            elif et == "catalog_gap_lead":
                clusters[ckey]["gap_leads"] += 1

        for q1, cand in syn_map.items():
            ckey = get_cluster_key(q1)
            if ckey not in clusters:
                clusters[ckey] = {
                    "cluster_key": ckey,
                    "queries": [q1],
                    "representative_query": q1,
                    "frequency": int(cand["support"]),
                    "hard_nulls": 0,
                    "soft_nulls": int(cand["support"]),
                    "abandons": 0,
                    "dislikes": 0,
                    "gap_leads": 0,
                }

        meaningful = [
            c
            for c in clusters.values()
            if (c["hard_nulls"] > 0 or c["soft_nulls"] > 0 or c["dislikes"] > 0 or c["gap_leads"] > 0 or c["abandons"] >= 2)
        ]

        ranked_clusters = sorted(
            meaningful,
            key=lambda c: (c["frequency"] + 2 * c["gap_leads"] + c["hard_nulls"]),
            reverse=True,
        )[:max_clusters]

        created_count = 0
        updated_count = 0
        processed_tickets: List[Dict[str, Any]] = []

        for cl in ranked_clusters:
            q_rep = cl["representative_query"]
            q_norm = norm_str(q_rep)
            entities = lookup_entity(q_norm)
            stat_hint = entities[0]["canonical_name"] if entities else q_norm
            cat_stat = catalog_stats(stat_hint)

            before_res = probe_search(q_rep, None, conn=conn)
            before_hit4 = float(before_res.get("hit4_score", 0.0))

            evidence: List[str] = [
                f"Частота в кластере: {cl['frequency']} (hard_null={cl['hard_nulls']}, soft_null={cl['soft_nulls']}, abandon={cl['abandons']}, 👎={cl['dislikes']}, gap_leads={cl['gap_leads']})"
            ]
            if q_norm in syn_map:
                evidence.append(syn_map[q_norm]["evidence"])
            if entities:
                evidence.append(f"Ближайшая сущность: {entities[0]['kind']} '{entities[0]['canonical_name']}' (score={entities[0]['match_score']})")
            if cat_stat.get("total_families", 0) > 0:
                evidence.append(f"Каталог по теме: {cat_stat['total_families']} семейств, доступно {cat_stat['available_families']}")

            diag = _rule_based_diagnose(cl, syn_map, entities, cat_stat, before_res)

            if llm_ready:
                llm_diag = _call_llm_diagnose(conn, cl, evidence, entities, cat_stat, before_res, diag)
                if llm_diag is not None:
                    # Не даём LLM потерять уже пойманную правилами проблему R1/R2 в R10
                    if diag["root_cause"] in ("R1", "R2") and llm_diag["root_cause"] == "R10":
                        pass
                    else:
                        if llm_diag.get("proposed_patch") is None and diag.get("proposed_patch") is not None and llm_diag["root_cause"] in ("R1", "R2"):
                            llm_diag["proposed_patch"] = diag["proposed_patch"]
                        diag = llm_diag
                        evidence.append("Диагноз уточнён LLM (deepseek-v4.1-flash)")

            patch = diag.get("proposed_patch")
            if patch is not None:
                after_res = probe_search(q_rep, patch, conn=conn)
                after_hit4 = float(after_res.get("hit4_score", before_hit4))
                top1_after = after_res["top4"][0]["canonical_name"] if after_res.get("top4") else "нет"
                evidence.append(
                    f"Dry-run ({before_res.get('mode')}): Hit@4 proxy {before_hit4:.2f} -> {after_hit4:.2f} (top-1 после: {top1_after[:60]})"
                )
            else:
                after_hit4 = before_hit4

            est_rub = estimate_monthly_rub(frequency=cl["frequency"])
            res_tkt = upsert_ticket(
                conn=conn,
                ticket_type=str(diag["ticket_type"]),
                root_cause=str(diag["root_cause"]),
                target_role=str(diag["target_role"]),
                cluster_key=cl["cluster_key"],
                affected_queries=cl["queries"],
                frequency=cl["frequency"],
                estimated_monthly_rub=est_rub,
                confidence=float(diag["confidence"]),
                summary=str(diag["summary"]),
                evidence=evidence,
                proposed_patch=patch,
                dry_run_before_hit4=before_hit4,
                dry_run_after_hit4=after_hit4,
            )
            if res_tkt["created"]:
                created_count += 1
            else:
                updated_count += 1

            processed_tickets.append(
                {
                    "ticket_id": res_tkt["ticket_id"],
                    "cluster_key": cl["cluster_key"],
                    "root_cause": str(diag["root_cause"]),
                    "ticket_type": str(diag["ticket_type"]),
                    "dry_run_before_hit4": before_hit4,
                    "dry_run_after_hit4": after_hit4,
                }
            )

        conn.commit()

    return {
        "status": "ok",
        "llm_used": llm_ready,
        "clusters_found": len(ranked_clusters),
        "created_tickets": created_count,
        "updated_tickets": updated_count,
        "tickets": processed_tickets,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Запуск диагностического агента (R1..R10 + dry-run)")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--max-clusters", type=int, default=15)
    args = parser.parse_args()
    out = run_agent_diagnose(use_llm=(not args.no_llm), max_clusters=args.max_clusters)
    print(json.dumps(out, ensure_ascii=False, indent=2))