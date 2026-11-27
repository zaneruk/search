"""Маскирование PII и запись в `events` / `search_traces` (B.3, B.6).

PII (B.3): email, телефоны и ИНН (10/12 цифр) заменяются масками `[email]`, `[phone]`, `[inn]`
ДО записи в БД. Значения под ключами-идентификаторами (`*_id`, `*_ids`, `url`, `ts` ...)
не маскируются, чтобы не портить `offer_id`/`family_id`/ссылки.

Если SQLite временно недоступна (режим L3), записи копятся в ограниченном буфере в памяти
и сбрасываются при следующей успешной записи; при переполнении самые старые отбрасываются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple, Union

from services.api.app.schemas import EventIn, EventType, SearchResponse
from services.api.core.cost_meter import connect_db, default_db_path, utc_now_iso

log = logging.getLogger("smart_search.events")

ALLOWED_SOURCES = ("live", "simulated", "gold_replay")

# --------------------------------------------------------------------------- ULID / идентификаторы
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(ts_ms: Optional[int] = None) -> str:
    """ULID (48 бит времени + 80 бит случайности), 26 символов Crockford base32."""
    ts = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
    value = ((ts & ((1 << 48) - 1)) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_trace_id() -> str:
    return "tr_" + new_ulid()


def new_event_id() -> str:
    return new_ulid()


# --------------------------------------------------------------------------- PII
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONE_RU_RE = re.compile(r"(?<![\d_A-Za-z])(?:\+7|8|7)[\s\-]*\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_PHONE_INTL_RE = re.compile(r"(?<!\d)\+\d{1,3}[\s\-]*\(?\d{2,4}\)?(?:[\s\-]*\d{2,4}){2,4}(?!\d)")
_INN_RE = re.compile(r"(?<![\d_A-Za-z])(?:\d{12}|\d{10})(?![\d_A-Za-z])")

_SAFE_KEYS = frozenset({"id", "url", "ts", "created_at", "updated_at", "image_url", "evidence_url", "href"})


def mask_pii(text: str) -> str:
    """Заменяет email, телефоны и ИНН (10/12 цифр) масками."""
    if not text:
        return text
    out = _EMAIL_RE.sub("[email]", text)
    out = _PHONE_RU_RE.sub("[phone]", out)
    out = _PHONE_INTL_RE.sub("[phone]", out)
    out = _INN_RE.sub("[inn]", out)
    return out


def _is_safe_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lower()
    return k in _SAFE_KEYS or k.endswith("_id") or k.endswith("_ids") or k.endswith("_url")


def mask_obj(obj: Any, _key: Any = None) -> Any:
    """Рекурсивно маскирует строки в dict/list; ключи-идентификаторы пропускаются."""
    if isinstance(obj, str):
        return obj if _is_safe_key(_key) else mask_pii(obj)
    if isinstance(obj, Mapping):
        return {k: mask_obj(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [mask_obj(v, _key) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# Поля SearchResponse, содержащие текст пользователя (каталожный контент не трогаем).
_RESPONSE_PII_FIELDS = ("query_raw", "query_norm", "applied_query", "did_you_mean", "related_queries", "intent_chips", "trace")


def mask_search_response(data: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    for field in _RESPONSE_PII_FIELDS:
        if field in out and out[field] is not None:
            out[field] = mask_obj(out[field], field)
    return out


# --------------------------------------------------------------------------- хранилище
class EventStore:
    def __init__(self, db_path: Optional[Union[str, Path]] = None, max_pending: int = 1000) -> None:
        self._db_path = str(db_path or default_db_path())
        self._pending: Deque[Tuple[str, Tuple[Any, ...]]] = deque()
        self._max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()
        self.dropped = 0

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # ------------------------------------------------------------------ низкий уровень
    def _take_pending(self) -> List[Tuple[str, Tuple[Any, ...]]]:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
            return items

    def _restore_pending(self, items: List[Tuple[str, Tuple[Any, ...]]]) -> None:
        with self._lock:
            for item in reversed(items):
                self._pending.appendleft(item)
            self._trim_locked()

    def _push_pending(self, sql: str, params: Tuple[Any, ...]) -> None:
        with self._lock:
            self._pending.append((sql, params))
            self._trim_locked()

    def _trim_locked(self) -> None:
        while len(self._pending) > self._max_pending:
            self._pending.popleft()
            self.dropped += 1

    def _write(self, sql: str, params: Tuple[Any, ...]) -> bool:
        """Пишет строку (и накопленный буфер) в одной транзакции. Возвращает True при успехе."""
        try:
            conn = connect_db(self._db_path)
            try:
                with conn:
                    pending = self._take_pending()
                    try:
                        for p_sql, p_params in pending:
                            conn.execute(p_sql, p_params)
                        conn.execute(sql, params)
                    except Exception:
                        self._restore_pending(pending)
                        raise
            finally:
                conn.close()
            return True
        except sqlite3.Error as exc:
            log.warning("events DB write failed, buffering (pending=%d): %s", self.pending_count + 1, exc)
            self._push_pending(sql, params)
            return False

    def flush_pending(self) -> int:
        """Пытается записать буфер. Возвращает число записанных строк."""
        pending = self._take_pending()
        if not pending:
            return 0
        try:
            conn = connect_db(self._db_path)
            try:
                with conn:
                    for sql, params in pending:
                        conn.execute(sql, params)
            finally:
                conn.close()
            return len(pending)
        except sqlite3.Error as exc:
            log.warning("events flush failed: %s", exc)
            self._restore_pending(pending)
            return 0

    # ------------------------------------------------------------------ events
    def write_event(
        self,
        event_type: Union[EventType, str],
        session_id: str,
        *,
        trace_id: Optional[str] = None,
        source: str = "live",
        payload: Optional[Mapping[str, Any]] = None,
        config_version: Optional[int] = None,
        event_id: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> str:
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"invalid event source: {source!r}")
        etype = event_type.value if isinstance(event_type, EventType) else str(event_type)
        eid = event_id or new_event_id()
        sql = (
            "INSERT OR IGNORE INTO events(event_id, ts, event_type, session_id, trace_id, source, config_version, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)"
        )
        params = (
            eid, ts or utc_now_iso(), etype, str(session_id), trace_id, source,
            int(config_version) if config_version is not None else None,
            _dumps(mask_obj(dict(payload or {}))),
        )
        self._write(sql, params)
        return eid

    def write_event_in(self, event: EventIn, config_version: Optional[int] = None) -> str:
        return self.write_event(
            event.event_type,
            event.session_id,
            trace_id=event.trace_id,
            source=event.source,
            payload=event.payload,
            config_version=config_version,
        )

    # ------------------------------------------------------------------ search_traces
    def write_search_trace(
        self,
        response: Union[SearchResponse, Mapping[str, Any]],
        *,
        session_id: str,
        source: str = "live",
        latency_ms: Optional[float] = None,
        ts: Optional[str] = None,
    ) -> str:
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"invalid trace source: {source!r}")
        data: Dict[str, Any] = response.model_dump(mode="json") if hasattr(response, "model_dump") else dict(response)
        masked = mask_search_response(data)
        trace_id = str(data["trace_id"])
        if latency_ms is None:
            latency_ms = sum(float(s.get("ms", 0.0)) for s in (data.get("trace") or []) if isinstance(s, Mapping))
        sql = (
            "INSERT OR REPLACE INTO search_traces(trace_id, ts, session_id, source, query_raw, query_norm, mode, "
            "degradation_level, config_version, hard_null, soft_null, fill_rung, latency_ms, response_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            trace_id,
            ts or utc_now_iso(),
            str(session_id),
            source,
            str(masked.get("query_raw", "")),
            str(masked.get("query_norm", "")),
            str(masked.get("mode", "smart")),
            str(getattr(masked.get("degradation_level"), "value", masked.get("degradation_level", "L0"))),
            int(masked.get("config_version", 0)),
            1 if masked.get("hard_null") else 0,
            1 if masked.get("soft_null") else 0,
            int(masked.get("fill_rung", 0)),
            round(float(latency_ms), 3),
            _dumps(masked),
        )
        self._write(sql, params)
        return trace_id

    # ------------------------------------------------------------------ async-обёртки
    async def awrite_event(self, *args: Any, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.write_event, *args, **kwargs)

    async def awrite_event_in(self, event: EventIn, config_version: Optional[int] = None) -> str:
        return await asyncio.to_thread(self.write_event_in, event, config_version)

    async def awrite_search_trace(self, *args: Any, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.write_search_trace, *args, **kwargs)


_event_store: Optional[EventStore] = None
_event_store_lock = threading.Lock()


def get_event_store() -> EventStore:
    global _event_store
    with _event_store_lock:
        if _event_store is None:
            _event_store = EventStore()
        return _event_store
