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
    action              TEXT NOT NULL,   -- 'init' | 'apply_ticket' | 'manual_edit' | 'rollback'
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