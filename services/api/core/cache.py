"""Кэш ответов LLM для QU (A.12: ключ = query_norm + ctx_bucket + overlay_version).

Два уровня: in-memory LRU (быстрый) и таблица `qu_cache` в SQLite WAL (переживает рестарт).
Любая ошибка SQLite трактуется как промах/пропуск записи: кэш никогда не ломает поиск
(в режиме L3, когда БД недоступна, вызывайте методы с `use_db=False`).

Переменные окружения: `QU_CACHE_MAX_ITEMS` (4096), `QU_CACHE_TTL_S` (604800 = 7 суток, 0 = без TTL).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, Union

from services.api.core.cost_meter import connect_db, default_db_path, utc_now_iso

log = logging.getLogger("smart_search.cache")


@dataclass(frozen=True)
class CacheEntry:
    response_json: str
    model: str
    created_at: str


def _parse_ts(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def make_cache_key(query_norm: str, ctx_bucket: str, overlay_version: int, *, namespace: str = "qu", extra: str = "") -> str:
    """Стабильный ключ: sha256 от (namespace, query_norm, ctx_bucket, overlay_version, extra).
    `extra` — версия промпта/схемы, чтобы смена промпта инвалидировала кэш."""
    raw = "\x1f".join((namespace, query_norm, ctx_bucket, str(int(overlay_version)), extra))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_ctx_bucket(segment: Optional[str], page_category_id: Optional[str] = None) -> str:
    """Грубая корзина контекста: сегмент и категория страницы (персона в ключ не входит)."""
    seg = getattr(segment, "value", segment) or "-"
    return f"{seg}|{page_category_id or '-'}"


class QuCache:
    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        max_items: Optional[int] = None,
        ttl_s: Optional[float] = None,
    ) -> None:
        self._db_path = str(db_path or default_db_path())
        self._max_items = max(1, int(max_items if max_items is not None else _env_int("QU_CACHE_MAX_ITEMS", 4096)))
        self._ttl_s = float(ttl_s if ttl_s is not None else _env_int("QU_CACHE_TTL_S", 7 * 86400))
        self._lru: "OrderedDict[str, Tuple[CacheEntry, float]]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits_memory = 0
        self._hits_db = 0
        self._misses = 0
        self._writes = 0

    # ------------------------------------------------------------------ memory layer
    def _expired(self, created_epoch: float) -> bool:
        return self._ttl_s > 0 and (time.time() - created_epoch) > self._ttl_s

    def _mem_put(self, key: str, entry: CacheEntry, created_epoch: float) -> None:
        with self._lock:
            self._lru[key] = (entry, created_epoch)
            self._lru.move_to_end(key)
            while len(self._lru) > self._max_items:
                self._lru.popitem(last=False)

    def _mem_get(self, key: str) -> Optional[CacheEntry]:
        with self._lock:
            item = self._lru.get(key)
            if item is None:
                return None
            entry, created_epoch = item
            if self._expired(created_epoch):
                self._lru.pop(key, None)
                return None
            self._lru.move_to_end(key)
            self._hits_memory += 1
            return entry

    # ------------------------------------------------------------------ public sync API
    def get(self, key: str, *, use_db: bool = True) -> Optional[CacheEntry]:
        entry = self._mem_get(key)
        if entry is not None:
            return entry
        if use_db:
            try:
                conn = connect_db(self._db_path)
                try:
                    row = conn.execute(
                        "SELECT response_json, model, created_at FROM qu_cache WHERE cache_key = ?", (key,)
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.warning("qu_cache read failed: %s", exc)
                row = None
            if row is not None:
                created_epoch = _parse_ts(row[2])
                if not self._expired(created_epoch):
                    entry = CacheEntry(response_json=row[0], model=row[1], created_at=row[2])
                    self._mem_put(key, entry, created_epoch)
                    with self._lock:
                        self._hits_db += 1
                    return entry
        with self._lock:
            self._misses += 1
        return None

    def set(self, key: str, response_json: str, model: str, *, use_db: bool = True) -> None:
        created_at = utc_now_iso()
        entry = CacheEntry(response_json=response_json, model=model, created_at=created_at)
        self._mem_put(key, entry, _parse_ts(created_at))
        with self._lock:
            self._writes += 1
        if not use_db:
            return
        try:
            conn = connect_db(self._db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO qu_cache(cache_key, response_json, model, created_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(cache_key) DO UPDATE SET response_json=excluded.response_json, "
                        "model=excluded.model, created_at=excluded.created_at",
                        (key, response_json, model, created_at),
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("qu_cache write failed: %s", exc)

    def delete(self, key: str, *, use_db: bool = True) -> None:
        with self._lock:
            self._lru.pop(key, None)
        if use_db:
            try:
                conn = connect_db(self._db_path)
                try:
                    with conn:
                        conn.execute("DELETE FROM qu_cache WHERE cache_key = ?", (key,))
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.warning("qu_cache delete failed: %s", exc)

    # ------------------------------------------------------------------ async wrappers
    async def aget(self, key: str, *, use_db: bool = True) -> Optional[CacheEntry]:
        entry = self._mem_get(key)
        if entry is not None:
            return entry
        if not use_db:
            with self._lock:
                self._misses += 1
            return None
        return await asyncio.to_thread(self.get, key, use_db=True)

    async def aset(self, key: str, response_json: str, model: str, *, use_db: bool = True) -> None:
        await asyncio.to_thread(self.set, key, response_json, model, use_db=use_db)

    # ------------------------------------------------------------------ maintenance
    def warm_from_db(self, limit: Optional[int] = None) -> int:
        """Подгружает самые свежие записи из SQLite в LRU (после рестарта). Возвращает число записей."""
        n = int(limit or self._max_items)
        try:
            conn = connect_db(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT cache_key, response_json, model, created_at FROM qu_cache ORDER BY created_at DESC LIMIT ?", (n,)
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("qu_cache warm failed: %s", exc)
            return 0
        loaded = 0
        for key, response_json, model, created_at in reversed(rows):
            epoch = _parse_ts(created_at)
            if self._expired(epoch):
                continue
            self._mem_put(key, CacheEntry(response_json, model, created_at), epoch)
            loaded += 1
        return loaded

    def warm(self, items: Iterable[Tuple[str, str, str]], *, use_db: bool = True) -> int:
        """Прогрев демо-сценариев: items = (cache_key, response_json, model)."""
        count = 0
        for key, response_json, model in items:
            self.set(key, response_json, model, use_db=use_db)
            count += 1
        return count

    def purge_expired(self) -> int:
        """Удаляет просроченные строки из SQLite и LRU. Возвращает число удалённых строк БД."""
        if self._ttl_s <= 0:
            return 0
        cutoff = datetime.fromtimestamp(time.time() - self._ttl_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            for key in [k for k, (_, ts) in self._lru.items() if self._expired(ts)]:
                self._lru.pop(key, None)
        try:
            conn = connect_db(self._db_path)
            try:
                with conn:
                    return max(0, conn.execute("DELETE FROM qu_cache WHERE created_at < ?", (cutoff,)).rowcount)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("qu_cache purge failed: %s", exc)
            return 0

    def clear_memory(self) -> None:
        with self._lock:
            self._lru.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            hits = self._hits_memory + self._hits_db
            total = hits + self._misses
            return {
                "size": len(self._lru),
                "max_items": self._max_items,
                "hits_memory": self._hits_memory,
                "hits_db": self._hits_db,
                "misses": self._misses,
                "writes": self._writes,
                "hit_rate": round(hits / total, 4) if total else 0.0,
            }


_qu_cache: Optional[QuCache] = None
_qu_cache_lock = threading.Lock()


def get_qu_cache() -> QuCache:
    global _qu_cache
    with _qu_cache_lock:
        if _qu_cache is None:
            _qu_cache = QuCache()
        return _qu_cache
