"""
Ограниченный набор инструментов диагностического агента (п. 4.4):
- query_failures: отбор провальных трасс и негативных событий из SQLite
- lookup_entity: поиск сущностей в entities.parquet, overlay и vendors_seed
- catalog_stats: статистика наличия и качества по вендору/типу в families.parquet
- probe_search: dry-run через POST /internal/probe (или локальный BM25F-фоллбэк по индексу)
"""

from __future__ import annotations

import json
import os
import pickle
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np
import pandas as pd
import pymorphy3
import scipy.sparse as sp
import yaml

from services.api.app.routers.admin_deps import get_artifacts_dir

_MORPH = pymorphy3.MorphAnalyzer()
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+(?:[-.][a-zа-яё0-9]+)*", re.IGNORECASE)
_CYR_RE = re.compile(r"[а-яё]")


def norm_str(text: Any) -> str:
    if text is None:
        return ""
    return " ".join(str(text).strip().lower().replace("ё", "е").split())


@lru_cache(maxsize=50_000)
def _lemma(tok: str) -> str:
    t = tok.lower().replace("ё", "е")
    if _CYR_RE.search(t) and len(t) >= 2:
        return _MORPH.parse(t)[0].normal_form.replace("ё", "е")
    return t


def tokenize_lemmas(text: str) -> List[str]:
    s = norm_str(text)
    out: List[str] = []
    for raw in _TOKEN_RE.findall(s):
        if "-" in raw:
            out.append(_lemma(raw))
            out.append(_lemma(raw.replace("-", "")))
            for part in raw.split("-"):
                if part:
                    out.append(_lemma(part))
        else:
            out.append(_lemma(raw))
    return [t for t in out if t]


class _LocalIndexCache:
    """Ленивый кэш артефактов для локального dry-run, если API /internal/probe не поднят."""

    def __init__(self) -> None:
        self.loaded_dir: Optional[Path] = None
        self.families_df: Optional[pd.DataFrame] = None
        self.entities_df: Optional[pd.DataFrame] = None
        self.bm25_mat: Optional[sp.csr_matrix] = None
        self.bm25_vocab: Dict[str, int] = {}
        self.fam_id_to_row: Dict[str, int] = {}

    def ensure_loaded(self) -> bool:
        art_dir = get_artifacts_dir()
        if art_dir is None:
            return False
        if self.loaded_dir == art_dir and self.families_df is not None:
            return True

        self.families_df = pd.read_parquet(art_dir / "families.parquet").reset_index(drop=True)
        self.entities_df = pd.read_parquet(art_dir / "entities.parquet")
        self.bm25_mat = sp.load_npz(art_dir / "bm25f.npz")
        with (art_dir / "bm25f_vocab.pkl").open("rb") as f:
            meta = pickle.load(f)
        self.bm25_vocab = meta.get("vocab", {})
        self.fam_id_to_row = {str(fid): i for i, fid in enumerate(self.families_df["family_id"].astype(str))}
        self.loaded_dir = art_dir
        return True


_LOCAL_IDX = _LocalIndexCache()


def query_failures(conn: sqlite3.Connection, limit: int = 1000) -> Dict[str, Any]:
    """Извлекает проблемные поисковые трассы (hard_null, soft_null) и негативные события (abandon, 👎, refine)."""
    failed_traces = [
        dict(r)
        for r in conn.execute(
            """
            SELECT trace_id, ts, session_id, source, query_raw, query_norm, mode,
                   hard_null, soft_null, fill_rung, latency_ms, response_json
            FROM search_traces
            WHERE mode = 'smart' AND (hard_null = 1 OR soft_null = 1)
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]

    neg_events = [
        dict(r)
        for r in conn.execute(
            """
            SELECT event_id, ts, event_type, session_id, trace_id, source, payload_json
            FROM events
            WHERE event_type IN ('abandon', 'feedback', 'catalog_gap_lead', 'refine')
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]

    # Дополняем трассы, по которым был abandon или дизлайк (feedback <= 0)
    bad_trace_ids = set()
    for ev in neg_events:
        if ev["event_type"] == "abandon" and ev["trace_id"]:
            bad_trace_ids.add(ev["trace_id"])
        elif ev["event_type"] == "feedback" and ev["trace_id"]:
            try:
                pl = json.loads(ev["payload_json"])
                if int(pl.get("rating", 1)) <= 0 or pl.get("liked") is False:
                    bad_trace_ids.add(ev["trace_id"])
            except Exception:
                pass

    seen_tids = {t["trace_id"] for t in failed_traces}
    missing_tids = list(bad_trace_ids - seen_tids)[:200]
    if missing_tids:
        placeholders = ",".join("?" for _ in missing_tids)
        extra = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT trace_id, ts, session_id, source, query_raw, query_norm, mode,
                       hard_null, soft_null, fill_rung, latency_ms, response_json
                FROM search_traces
                WHERE mode = 'smart' AND trace_id IN ({placeholders})
                """,
                tuple(missing_tids),
            ).fetchall()
        ]
        failed_traces.extend(extra)

    return {
        "failed_traces": failed_traces,
        "negative_events": neg_events,
        "bad_trace_ids": bad_trace_ids,
    }


def lookup_entity(query_norm: str) -> List[Dict[str, Any]]:
    """Ищет похожие сущности в entities.parquet (BRAND, FAMILY, CATEGORY, CONTENT, EXTERNAL_PRODUCT)."""
    if not _LOCAL_IDX.ensure_loaded() or _LOCAL_IDX.entities_df is None:
        return []
    q = norm_str(query_norm)
    q_toks = set(tokenize_lemmas(q))
    if not q:
        return []

    hits: List[Tuple[float, Dict[str, Any]]] = []
    edf = _LOCAL_IDX.entities_df
    for _, er in edf.iterrows():
        cname = norm_str(er["canonical_name"])
        aliases_raw = json.loads(er["aliases_json"]) if er["aliases_json"] else []
        aliases_norm = [norm_str(a) for a in aliases_raw]

        score = 0.0
        if q == cname or q in aliases_norm:
            score = 1.0
        elif any((q in a or a in q) for a in ([cname] + aliases_norm) if len(a) >= 3):
            score = 0.75
        else:
            ent_toks = set(tokenize_lemmas(cname + " " + " ".join(aliases_norm[:6])))
            if q_toks and ent_toks:
                overlap = len(q_toks & ent_toks) / len(q_toks)
                if overlap >= 0.5:
                    score = 0.5 * overlap

        if score >= 0.35:
            hits.append(
                (
                    score,
                    {
                        "entity_id": str(er["entity_id"]),
                        "kind": str(er["kind"]),
                        "canonical_name": str(er["canonical_name"]),
                        "aliases": aliases_raw[:6],
                        "target_family_ids": json.loads(er["target_family_ids_json"])[:10],
                        "url": er["url"] if pd.notna(er["url"]) else None,
                        "match_score": round(score, 3),
                    },
                )
            )

    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:8]]


def catalog_stats(vendor_or_hint: str) -> Dict[str, Any]:
    """Проверяет статистику каталога по бренду/подстроке (доступность, число семейств, качество)."""
    if not _LOCAL_IDX.ensure_loaded() or _LOCAL_IDX.families_df is None:
        return {"total_families": 0, "available_families": 0}
    fdf = _LOCAL_IDX.families_df
    hint = norm_str(vendor_or_hint)
    if not hint:
        return {"total_families": len(fdf), "available_families": int(fdf["available_any"].sum())}

    toks = [t for t in hint.split() if len(t) >= 3]
    if not toks:
        return {"total_families": 0, "available_families": 0}

    mask = pd.Series(False, index=fdf.index)
    v_col = fdf["vendor"].fillna("").str.lower()
    n_col = fdf["canonical_name"].fillna("").str.lower()
    for t in toks[:3]:
        mask = mask | v_col.str.contains(re.escape(t), regex=True) | n_col.str.contains(re.escape(t), regex=True)

    sub = fdf[mask]
    if len(sub) == 0:
        return {"total_families": 0, "available_families": 0, "sample_names": []}

    return {
        "total_families": int(len(sub)),
        "available_families": int(sub["available_any"].sum()),
        "priced_families": int(sub["price_min_minor"].notna().sum()),
        "avg_quality_score": round(float(sub["data_quality_score"].mean()), 3),
        "sample_names": sub.sort_values("pop_prior", ascending=False)["canonical_name"].head(5).tolist(),
        "top_family_ids": sub.sort_values(["available_any", "pop_prior"], ascending=[False, False])["family_id"]
        .head(5)
        .astype(str)
        .tolist(),
    }


def _load_active_overlay_from_db(conn: Optional[sqlite3.Connection]) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    rows = conn.execute("SELECT kind, key_norm, value_json FROM overlay_items WHERE active = 1").fetchall()
    return [{"kind": r["kind"], "key": r["key_norm"], "value": json.loads(r["value_json"])} for r in rows]


def _local_probe_search(
    query: str,
    temp_patch: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Быстрый локальный dry-run по BM25F + entity + overlay (используется, если API /internal/probe ещё не запущен).
    """
    if not _LOCAL_IDX.ensure_loaded() or _LOCAL_IDX.families_df is None or _LOCAL_IDX.bm25_mat is None:
        return {"mode": "unavailable", "hard_null": True, "soft_null": True, "top4": [], "hit4_score": 0.0}

    fdf = _LOCAL_IDX.families_df
    q_norm = norm_str(query)
    overlay_rules = _load_active_overlay_from_db(conn)
    if temp_patch:
        overlay_rules.append(
            {
                "kind": temp_patch.get("kind"),
                "key": norm_str(temp_patch.get("key", "")),
                "value": temp_patch.get("value") or {},
            }
        )

    # Применяем синонимы / алиасы / pin / demote из overlay + временного патча
    expanded_queries = [q_norm]
    pinned_fids: List[str] = []
    demoted_fids: set[str] = set()
    boost_fids: set[str] = set()

    for rule in overlay_rules:
        r_kind = rule.get("kind")
        r_key = norm_str(rule.get("key", ""))
        r_val = rule.get("value") or {}
        if r_key and (r_key == q_norm or r_key in q_norm):
            if r_kind in ("synonym", "alias"):
                for t in (r_val.get("synonyms") or r_val.get("targets") or []):
                    expanded_queries.append(norm_str(t))
                if r_val.get("target"):
                    expanded_queries.append(norm_str(r_val["target"]))
            elif r_kind == "pin" and r_val.get("family_id"):
                pinned_fids.append(str(r_val["family_id"]))
            elif r_kind == "boost" and r_val.get("family_id"):
                boost_fids.add(str(r_val["family_id"]))
            elif r_kind == "demote" and r_val.get("family_id"):
                demoted_fids.add(str(r_val["family_id"]))

    # Леммы по всем расширенным запросам
    all_lemmas: List[str] = []
    for eq in expanded_queries:
        all_lemmas.extend(tokenize_lemmas(eq))

    cols = [_LOCAL_IDX.bm25_vocab[t] for t in set(all_lemmas) if t in _LOCAL_IDX.bm25_vocab]
    scores = np.zeros(len(fdf), dtype=np.float32)
    if cols:
        scores = np.asarray(_LOCAL_IDX.bm25_mat[:, cols].sum(axis=1)).ravel()

    # Учитываем pin / boost / demote
    for fid in boost_fids:
        idx = _LOCAL_IDX.fam_id_to_row.get(fid)
        if idx is not None:
            scores[idx] += 5.0
    for fid in pinned_fids:
        idx = _LOCAL_IDX.fam_id_to_row.get(fid)
        if idx is not None:
            scores[idx] += 50.0
    for fid in demoted_fids:
        idx = _LOCAL_IDX.fam_id_to_row.get(fid)
        if idx is not None:
            scores[idx] -= 50.0

    # Проверка сильного попадания
    top_idx = np.argpartition(-scores, kth=min(10, len(scores) - 1))[:10]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    positive = [int(i) for i in top_idx if scores[i] > 0.5]
    hard_null = len(positive) == 0
    best_score = float(scores[top_idx[0]]) if len(top_idx) > 0 else 0.0
    soft_null = hard_null or best_score < 2.0

    top4 = []
    for i in positive[:4]:
        row = fdf.iloc[i]
        top4.append(
            {
                "family_id": str(row["family_id"]),
                "canonical_name": str(row["canonical_name"]),
                "vendor": str(row["vendor"]),
                "score": round(float(scores[i]), 3),
            }
        )

    # Оценка dry-run Hit@4 proxy (1.0 если есть уверенное совпадение без null, 0.0 если null)
    hit4_proxy = 0.0 if soft_null else round(min(1.0, best_score / 6.0), 2)
    if pinned_fids and not hard_null:
        hit4_proxy = 1.0

    return {
        "mode": "local_bm25f_fallback",
        "hard_null": hard_null,
        "soft_null": soft_null,
        "best_score": round(best_score, 3),
        "top4": top4,
        "hit4_score": hit4_proxy,
    }


def probe_search(
    query: str,
    temp_patch: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Контрфактическая проверка (dry-run) запроса с временным патчем (A.8).
    Сначала пробует POST /internal/probe Контура 1; при недоступности — локальный индекс.
    """
    api_url = os.getenv("INTERNAL_API_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.getenv("INTERNAL_TOKEN", "dev-internal-token")
    try:
        with httpx.Client(timeout=2.5) as client:
            resp = client.post(
                f"{api_url}/internal/probe",
                headers={"X-Internal-Token": token},
                json={"query": query, "temp_patch": temp_patch},
            )
            if resp.status_code == 200:
                data = resp.json()
                items = []
                for sec in data.get("sections", []) or []:
                    items.extend(sec.get("items", []) or [])
                hard_null = bool(data.get("hard_null", False))
                soft_null = bool(data.get("soft_null", False))
                top4 = [
                    {
                        "family_id": it.get("family_id"),
                        "canonical_name": it.get("canonical_name"),
                        "vendor": it.get("vendor"),
                        "score": it.get("score"),
                        "relevance_label": it.get("relevance_label"),
                    }
                    for it in items[:4]
                ]
                exact_or_close = sum(1 for it in items[:4] if it.get("relevance_label") in ("exact", "close"))
                hit4_score = 0.0 if hard_null else (1.0 if exact_or_close > 0 and not soft_null else 0.5)
                return {
                    "mode": "http_internal_probe",
                    "hard_null": hard_null,
                    "soft_null": soft_null,
                    "top4": top4,
                    "hit4_score": hit4_score,
                }
    except Exception:
        pass

    return _local_probe_search(query=query, temp_patch=temp_patch, conn=conn)