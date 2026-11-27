"""
Общие утилиты для admin-роутеров Контура 3.
Работает строго поверх SQLite (WAL) и совместим как с Windows (CURRENT_VERSION),
так и с Linux/Docker (симлинк data/artifacts/current).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import yaml
from fastapi import Header, HTTPException, Request, status

# Запасной DDL строго из CONTRACTS.md B.6 (на случай, если 001_init.sql ещё не применён)
FALLBACK_INIT_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    trace_id        TEXT,
    source          TEXT NOT NULL CHECK (source IN ('live', 'simulated', 'gold_replay')),
    config_version  INTEGER,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_type_src ON events(event_type, source);

CREATE TABLE IF NOT EXISTS search_traces (
    trace_id            TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    source              TEXT NOT NULL,
    query_raw           TEXT NOT NULL,
    query_norm          TEXT NOT NULL,
    mode                TEXT NOT NULL,
    degradation_level   TEXT NOT NULL,
    config_version      INTEGER NOT NULL,
    hard_null           INTEGER NOT NULL,
    soft_null           INTEGER NOT NULL,
    fill_rung           INTEGER NOT NULL,
    latency_ms          REAL NOT NULL,
    response_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_nulls ON search_traces(hard_null, soft_null, ts);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    usd_per_mtok_in     REAL NOT NULL,
    usd_per_mtok_cached REAL NOT NULL,
    usd_per_mtok_out    REAL NOT NULL,
    usd_to_rub          REAL NOT NULL,
    price_source_url    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    tokens_in           INTEGER NOT NULL,
    tokens_cached       INTEGER NOT NULL,
    tokens_out          INTEGER NOT NULL,
    latency_ms          REAL NOT NULL,
    price_snapshot_id   TEXT NOT NULL REFERENCES price_snapshots(snapshot_id),
    cost_micro_rub      INTEGER NOT NULL,
    trace_id            TEXT,
    status              TEXT NOT NULL,
    retry_n             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cost_ts_purpose ON cost_ledger(ts, purpose);

CREATE TABLE IF NOT EXISTS qu_cache (
    cache_key           TEXT PRIMARY KEY,
    response_json       TEXT NOT NULL,
    model               TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    config_version      INTEGER PRIMARY KEY,
    created_at          TEXT NOT NULL,
    author              TEXT NOT NULL,
    ticket_id           TEXT,
    action              TEXT NOT NULL,
    comment             TEXT,
    overlay_snapshot_json TEXT NOT NULL,
    ranking_weights_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overlay_items (
    item_id             TEXT PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('synonym', 'alias', 'pin', 'demote', 'boost', 'catalog_patch')),
    key_norm            TEXT NOT NULL,
    value_json          TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    config_version      INTEGER NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_overlay_active ON overlay_items(active, kind, key_norm);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id               TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    ticket_type             TEXT NOT NULL,
    root_cause              TEXT NOT NULL,
    target_role             TEXT NOT NULL,
    cluster_key             TEXT NOT NULL,
    affected_queries_json   TEXT NOT NULL,
    frequency               INTEGER NOT NULL,
    estimated_monthly_rub   INTEGER NOT NULL,
    confidence              REAL NOT NULL,
    summary                 TEXT NOT NULL,
    evidence_json           TEXT NOT NULL,
    proposed_patch_json     TEXT,
    dry_run_before_hit4     REAL,
    dry_run_after_hit4      REAL,
    status                  TEXT NOT NULL CHECK (status IN ('open', 'applied', 'rejected', 'rolled_back')),
    applied_config_version  INTEGER
);
"""

DEFAULT_RANKING_WEIGHTS: Dict[str, float] = {
    "rel_fused": 1.0,
    "exact_name_match": 0.65,
    "availability": 0.25,
    "popularity": 0.20,
    "margin_proxy": 0.12,
    "promo": 0.10,
    "version_recency": 0.10,
    "segment_fit": 0.18,
}

# Глобальное состояние chaos-тумблеров на случай запуска роутеров без app.state.degradation
_FALLBACK_CHAOS_STATE: Dict[str, Any] = {
    "force_disable_llm": False,
    "force_disable_dense": False,
    "force_disable_overlay": False,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db_path() -> Path:
    return Path(os.getenv("SQLITE_PATH", "data/db/smart_search.sqlite"))


def get_artifacts_dir() -> Optional[Path]:
    root = Path(os.getenv("ARTIFACTS_ROOT", "data/artifacts"))
    current_link = root / "current"
    if current_link.exists() and current_link.is_dir():
        return current_link
    ver_file = root / "CURRENT_VERSION"
    if ver_file.exists():
        ver = ver_file.read_text(encoding="utf-8").strip()
        candidate = root / ver
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def get_index_version() -> str:
    art_dir = get_artifacts_dir()
    if art_dir is None:
        return "idx_not_built"
    manifest_p = art_dir / "manifest.json"
    if manifest_p.exists():
        try:
            return str(json.loads(manifest_p.read_text(encoding="utf-8")).get("index_version", art_dir.name))
        except Exception:
            pass
    ver_file = Path(os.getenv("ARTIFACTS_ROOT", "data/artifacts")) / "CURRENT_VERSION"
    if ver_file.exists():
        return ver_file.read_text(encoding="utf-8").strip()
    return art_dir.name


def load_default_ranking_weights() -> Dict[str, float]:
    cfg_path = Path(os.getenv("SEARCH_CONFIG_PATH", "config/search.yaml"))
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            rw = data.get("ranking_weights") or data.get("ranking", {}).get("weights")
            if isinstance(rw, dict) and rw:
                merged = dict(DEFAULT_RANKING_WEIGHTS)
                for k, v in rw.items():
                    merged[str(k)] = float(v)
                return merged
        except Exception:
            pass
    return dict(DEFAULT_RANKING_WEIGHTS)


def ensure_db_initialized(conn: sqlite3.Connection) -> None:
    sql_path = Path("contracts/sql/001_init.sql")
    ddl = sql_path.read_text(encoding="utf-8") if sql_path.exists() else FALLBACK_INIT_SQL
    conn.executescript(ddl)

    # Гарантируем базовый price_snapshot (чтобы cost_ledger не падал по FK)
    cur = conn.execute("SELECT COUNT(*) FROM price_snapshots")
    if cur.fetchone()[0] == 0:
        conn.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id, created_at, provider, model,
                usd_per_mtok_in, usd_per_mtok_cached, usd_per_mtok_out,
                usd_to_rub, price_source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "snap_default_mvp",
                utc_now_iso(),
                "openrouter",
                "openai/gpt-4o-mini",
                0.15,
                0.075,
                0.60,
                100.0,
                "https://openrouter.ai/models/openai/gpt-4o-mini",
            ),
        )

    # Гарантируем config_version = 1
    cur_cv = conn.execute("SELECT COUNT(*) FROM config_versions")
    if cur_cv.fetchone()[0] == 0:
        conn.execute(
            """
            INSERT INTO config_versions (
                config_version, created_at, author, ticket_id,
                action, comment, overlay_snapshot_json, ranking_weights_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                utc_now_iso(),
                "system",
                None,
                "init",
                "Начальная конфигурация MVP",
                "[]",
                json.dumps(load_default_ranking_weights(), ensure_ascii=False),
            ),
        )
    conn.commit()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        ensure_db_initialized(conn)
        yield conn
    finally:
        conn.close()


def require_admin_token(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")) -> str:
    expected = os.getenv("ADMIN_TOKEN", "dev-admin-token").strip()
    if not expected:
        return "ok"
    if not x_admin_token or x_admin_token.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "UNAUTHORIZED",
                "message": "Неверный или отсутствующий заголовок X-Admin-Token",
                "trace_id": None,
                "details": None,
            },
        )
    return x_admin_token


def get_current_config_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(config_version) AS v FROM config_versions").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 1


def get_chaos_state(request: Optional[Request] = None) -> Dict[str, Any]:
    if request is not None:
        deg = getattr(request.app.state, "degradation", None)
        if deg is not None:
            return {
                "force_disable_llm": bool(getattr(deg, "force_disable_llm", False)),
                "force_disable_dense": bool(getattr(deg, "force_disable_dense", False)),
                "force_disable_overlay": bool(getattr(deg, "force_disable_overlay", False)),
                "degradation_level": str(
                    deg.current_level().value
                    if hasattr(deg, "current_level") and hasattr(deg.current_level(), "value")
                    else getattr(deg, "level", "L0")
                ),
            }
    llm_off = bool(_FALLBACK_CHAOS_STATE["force_disable_llm"])
    dense_off = bool(_FALLBACK_CHAOS_STATE["force_disable_dense"])
    overlay_off = bool(_FALLBACK_CHAOS_STATE["force_disable_overlay"])
    level = "L3" if overlay_off else ("L2" if dense_off else ("L1" if llm_off else "L0"))
    return {
        "force_disable_llm": llm_off,
        "force_disable_dense": dense_off,
        "force_disable_overlay": overlay_off,
        "degradation_level": level,
    }


def set_chaos_state(
    request: Optional[Request],
    force_disable_llm: Optional[bool] = None,
    force_disable_dense: Optional[bool] = None,
    force_disable_overlay: Optional[bool] = None,
    degradation_level: Optional[str] = None,
) -> Dict[str, Any]:
    if degradation_level is not None:
        lvl = degradation_level.upper()
        if lvl == "L0":
            force_disable_llm, force_disable_dense, force_disable_overlay = False, False, False
        elif lvl == "L1":
            force_disable_llm, force_disable_dense, force_disable_overlay = True, False, False
        elif lvl == "L2":
            force_disable_llm, force_disable_dense, force_disable_overlay = True, True, False
        elif lvl == "L3":
            force_disable_overlay = True

    if force_disable_llm is not None:
        _FALLBACK_CHAOS_STATE["force_disable_llm"] = bool(force_disable_llm)
    if force_disable_dense is not None:
        _FALLBACK_CHAOS_STATE["force_disable_dense"] = bool(force_disable_dense)
    if force_disable_overlay is not None:
        _FALLBACK_CHAOS_STATE["force_disable_overlay"] = bool(force_disable_overlay)

    if request is not None:
        deg = getattr(request.app.state, "degradation", None)
        if deg is not None:
            if hasattr(deg, "set_chaos"):
                deg.set_chaos(
                    force_disable_llm=_FALLBACK_CHAOS_STATE["force_disable_llm"],
                    force_disable_dense=_FALLBACK_CHAOS_STATE["force_disable_dense"],
                    force_disable_overlay=_FALLBACK_CHAOS_STATE["force_disable_overlay"],
                )
            else:
                if force_disable_llm is not None and hasattr(deg, "force_disable_llm"):
                    deg.force_disable_llm = bool(force_disable_llm)
                if force_disable_dense is not None and hasattr(deg, "force_disable_dense"):
                    deg.force_disable_dense = bool(force_disable_dense)
                if force_disable_overlay is not None and hasattr(deg, "force_disable_overlay"):
                    deg.force_disable_overlay = bool(force_disable_overlay)

    return get_chaos_state(request)


def notify_overlay_reload(request: Optional[Request], conn: sqlite3.Connection) -> None:
    """Сигнализирует Контуру 1 (OverlayState) об атомарном hot-swap после изменения БД."""
    if request is None:
        return
    overlay = getattr(request.app.state, "overlay", None)
    if overlay is not None and hasattr(overlay, "reload"):
        try:
            overlay.reload()
        except Exception:
            pass