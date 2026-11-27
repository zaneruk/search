"""Overlay правил и весов ранжирования (B.6: overlay_items + config_versions).

* `OverlaySnapshot` — неизменяемый снимок (frozen dataclass, MappingProxyType, tuple).
  Читатели берут ссылку `store.snapshot()` без блокировок и работают с согласованным состоянием.
* `OverlayStore.hot_swap()` — атомарная подмена ссылки на снимок.
* Каждая запись (правила, веса, откат) создаёт НОВУЮ строку `config_versions` (+1) в одной
  транзакции `BEGIN IMMEDIATE`, затем горячо подменяет снимок.
* `maybe_refresh()` подхватывает версии, записанные другим процессом (worker).

`item_id` по умолчанию детерминирован: `ov_<sha1(kind|key_norm)[:16]>`, то есть одно правило
на пару (kind, key_norm); повторная запись обновляет правило.
Веса по умолчанию берутся из `config/search.yaml` (ключ `ranking_weights`), иначе из FALLBACK_WEIGHTS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from services.api.app.schemas import OverlayRulePayload
from services.api.core.cost_meter import connect_db, default_db_path, utc_now_iso

log = logging.getLogger("smart_search.overlay")

ALLOWED_KINDS = frozenset({"synonym", "alias", "pin", "demote", "boost", "catalog_patch"})

FALLBACK_WEIGHTS: Dict[str, float] = {
    "rel_fused": 1.0,
    "exact_name_match": 0.5,
    "availability": 0.3,
    "popularity": 0.2,
    "margin_proxy": 0.1,
    "promo": 0.1,
    "version_recency": 0.1,
    "segment_fit": 0.15,
}

# публичные имена админ-эндпоинта -> имена признаков ранжирования
ADMIN_WEIGHT_ALIASES = {
    "margin_weight": "margin_proxy",
    "promo_weight": "promo",
    "pop_weight": "popularity",
}


# --------------------------------------------------------------------------- утилиты
def normalize_key(value: str) -> str:
    """NFKC, нижний регистр, ё->е, схлопывание пробелов (совместимо с query_norm из B.3)."""
    s = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return " ".join(s.split())


def make_item_id(kind: str, key_norm: str) -> str:
    return "ov_" + hashlib.sha1(f"{kind}|{key_norm}".encode("utf-8")).hexdigest()[:16]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _clean_weights(raw: Mapping[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key, val in raw.items():
        try:
            num = float(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"weight {key!r} is not a number: {val!r}") from exc
        if not math.isfinite(num) or num < 0:
            raise ValueError(f"weight {key!r} must be finite and >= 0, got {val!r}")
        out[str(key)] = num
    return out


def load_default_weights(config_path: Optional[Union[str, Path]] = None) -> Dict[str, float]:
    weights = dict(FALLBACK_WEIGHTS)
    path = Path(config_path or os.getenv("SEARCH_CONFIG_PATH", "config/search.yaml"))
    if not path.is_file():
        return weights
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = None
        if isinstance(data, dict):
            cfg = data.get("ranking_weights")
            if cfg is None and isinstance(data.get("ranking"), dict):
                cfg = data["ranking"].get("weights")
        if isinstance(cfg, dict):
            weights.update(_clean_weights(cfg))
    except Exception as exc:  # noqa: BLE001 - битый конфиг не должен ронять API
        log.warning("cannot read ranking_weights from %s: %s", path, exc)
    return weights


# --------------------------------------------------------------------------- снимок
@dataclass(frozen=True, eq=False)
class OverlayItem:
    item_id: str
    kind: str
    key_norm: str
    value: Mapping[str, Any]
    active: bool
    config_version: int
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "key_norm": self.key_norm,
            "value": _thaw(self.value),
            "active": self.active,
            "config_version": self.config_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, eq=False)
class OverlaySnapshot:
    config_version: int
    created_at: str
    items: Tuple[OverlayItem, ...]
    ranking_weights: Mapping[str, float]
    _index: Mapping[str, Mapping[str, Tuple[OverlayItem, ...]]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        idx: Dict[str, Dict[str, List[OverlayItem]]] = {}
        for item in self.items:
            if item.active:
                idx.setdefault(item.kind, {}).setdefault(item.key_norm, []).append(item)
        frozen = MappingProxyType(
            {k: MappingProxyType({kk: tuple(vv) for kk, vv in d.items()}) for k, d in idx.items()}
        )
        object.__setattr__(self, "_index", frozen)
        object.__setattr__(self, "ranking_weights", MappingProxyType(dict(self.ranking_weights)))
        object.__setattr__(self, "items", tuple(self.items))

    @classmethod
    def empty(cls, weights: Optional[Mapping[str, float]] = None, config_version: int = 0) -> "OverlaySnapshot":
        return cls(
            config_version=config_version,
            created_at=utc_now_iso(),
            items=(),
            ranking_weights=dict(weights if weights is not None else FALLBACK_WEIGHTS),
        )

    @property
    def is_empty(self) -> bool:
        return not self.items

    def rules(self, kind: str) -> Mapping[str, Tuple[OverlayItem, ...]]:
        """Активные правила данного вида: key_norm -> (items...)."""
        return self._index.get(kind, MappingProxyType({}))

    def get(self, kind: str, key_norm: str) -> Tuple[OverlayItem, ...]:
        return self._index.get(kind, {}).get(key_norm, ())

    def weight(self, name: str, default: float = 0.0) -> float:
        return float(self.ranking_weights.get(name, default))

    def items_json(self) -> str:
        return json.dumps([i.to_dict() for i in self.items if i.active], ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- хранилище
_Mutation = Callable[[sqlite3.Connection, int, int, Dict[str, float]], Optional[Dict[str, float]]]


class OverlayStore:
    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        default_weights: Optional[Mapping[str, float]] = None,
        refresh_interval_s: float = 1.0,
    ) -> None:
        self._db_path = str(db_path or default_db_path())
        self._defaults: Dict[str, float] = dict(default_weights) if default_weights is not None else load_default_weights()
        self._refresh_interval_s = float(refresh_interval_s)
        self._lock = threading.RLock()
        self._snapshot: OverlaySnapshot = OverlaySnapshot.empty(self._defaults)
        self._last_check = 0.0
        self.loaded_ok = False

    # ------------------------------------------------------------------ чтение
    def snapshot(self) -> OverlaySnapshot:
        """Текущий снимок (чтение ссылки атомарно, блокировка не нужна)."""
        return self._snapshot

    def base_snapshot(self) -> OverlaySnapshot:
        """Пустой снимок для режима L3 (без overlay и БД)."""
        return OverlaySnapshot.empty(self._defaults)

    def read_weights(self) -> Mapping[str, float]:
        return self._snapshot.ranking_weights

    # ------------------------------------------------------------------ hot-swap
    def hot_swap(self, new: OverlaySnapshot, *, allow_downgrade: bool = False) -> OverlaySnapshot:
        """Атомарно подменяет снимок и возвращает предыдущий."""
        with self._lock:
            old = self._snapshot
            if not allow_downgrade and old.config_version > 0 and new.config_version < old.config_version:
                raise ValueError(f"refusing to hot-swap to older config_version {new.config_version} < {old.config_version}")
            self._snapshot = new
            return old

    # ------------------------------------------------------------------ загрузка из БД
    def _merge_weights(self, weights_json: Optional[str]) -> Dict[str, float]:
        merged = dict(self._defaults)
        if weights_json:
            try:
                parsed = json.loads(weights_json)
                if isinstance(parsed, dict):
                    merged.update(_clean_weights(parsed))
            except (ValueError, TypeError) as exc:
                log.warning("bad ranking_weights_json, using defaults: %s", exc)
        return merged

    def _ensure_init_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT 1 FROM config_versions LIMIT 1").fetchone()
        if row is None:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO config_versions(config_version, created_at, author, ticket_id, action, comment, "
                    "overlay_snapshot_json, ranking_weights_json) VALUES (1, ?, 'system', NULL, 'init', 'initial config', '[]', ?)",
                    (utc_now_iso(), json.dumps(self._defaults, separators=(",", ":"))),
                )

    def _read_snapshot(self, conn: sqlite3.Connection) -> OverlaySnapshot:
        row = conn.execute(
            "SELECT config_version, created_at, ranking_weights_json FROM config_versions ORDER BY config_version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return OverlaySnapshot.empty(self._defaults)
        version, created_at, weights_json = int(row[0]), str(row[1]), row[2]
        items: List[OverlayItem] = []
        for r in conn.execute(
            "SELECT item_id, kind, key_norm, value_json, active, config_version, updated_at FROM overlay_items "
            "WHERE active = 1 ORDER BY item_id"
        ):
            try:
                value = json.loads(r[3])
            except (ValueError, TypeError):
                log.warning("overlay item %s has invalid value_json, skipped", r[0])
                continue
            if not isinstance(value, dict):
                value = {"value": value}
            items.append(OverlayItem(str(r[0]), str(r[1]), str(r[2]), _freeze(value), bool(r[4]), int(r[5]), str(r[6])))
        return OverlaySnapshot(
            config_version=version,
            created_at=created_at,
            items=tuple(items),
            ranking_weights=self._merge_weights(weights_json),
        )

    def load(self) -> bool:
        """Загружает последнюю версию из SQLite и горячо подменяет снимок.
        При ошибке БД текущий снимок сохраняется, возвращается False."""
        try:
            conn = connect_db(self._db_path)
            try:
                self._ensure_init_version(conn)
                snap = self._read_snapshot(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.error("overlay load failed: %s", exc)
            return False
        self.hot_swap(snap, allow_downgrade=True)
        self.loaded_ok = True
        self._last_check = time.monotonic()
        return True

    def maybe_refresh(self, *, force: bool = False) -> bool:
        """Перечитывает overlay, если в БД появилась другая версия (запись из другого процесса)."""
        now = time.monotonic()
        if not force and now - self._last_check < self._refresh_interval_s:
            return False
        self._last_check = now
        try:
            conn = connect_db(self._db_path)
            try:
                row = conn.execute("SELECT MAX(config_version) FROM config_versions").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        latest = row[0] if row else None
        if latest is None or int(latest) == self._snapshot.config_version:
            return False
        return self.load()

    # ------------------------------------------------------------------ запись версий
    @staticmethod
    def _active_items_json(conn: sqlite3.Connection) -> str:
        items = []
        for r in conn.execute(
            "SELECT item_id, kind, key_norm, value_json, config_version, updated_at FROM overlay_items WHERE active = 1 ORDER BY item_id"
        ):
            try:
                value = json.loads(r[3])
            except (ValueError, TypeError):
                continue
            items.append({
                "item_id": r[0], "kind": r[1], "key_norm": r[2], "value": value,
                "active": True, "config_version": r[4], "updated_at": r[5],
            })
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    def _commit(
        self,
        mutate: _Mutation,
        *,
        author: str,
        action: str,
        ticket_id: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> OverlaySnapshot:
        """Создаёт новую версию конфига в одной транзакции и горячо подменяет снимок."""
        with self._lock:
            conn = connect_db(self._db_path)
            conn.isolation_level = None  # явное управление транзакцией
            try:
                self._ensure_init_version(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "SELECT config_version, ranking_weights_json FROM config_versions ORDER BY config_version DESC LIMIT 1"
                    ).fetchone()
                    current_version = int(cur[0]) if cur else 0
                    weights = self._merge_weights(cur[1] if cur else None)
                    new_version = current_version + 1
                    new_weights = mutate(conn, current_version, new_version, dict(weights))
                    final_weights = _clean_weights(new_weights) if new_weights is not None else weights
                    conn.execute(
                        "INSERT INTO config_versions(config_version, created_at, author, ticket_id, action, comment, "
                        "overlay_snapshot_json, ranking_weights_json) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            new_version, utc_now_iso(), author, ticket_id, action, comment,
                            self._active_items_json(conn),
                            json.dumps(final_weights, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            conn2 = connect_db(self._db_path)
            try:
                snap = self._read_snapshot(conn2)
            finally:
                conn2.close()
            self.hot_swap(snap, allow_downgrade=True)
            return snap

    def apply_rules(
        self,
        rules: Sequence[Union[OverlayRulePayload, Mapping[str, Any]]],
        *,
        author: str = "system",
        ticket_id: Optional[str] = None,
        action: str = "apply_ticket",
        comment: Optional[str] = None,
    ) -> OverlaySnapshot:
        """Добавляет/обновляет правила overlay и публикует новую config_version."""
        prepared: List[Tuple[str, str, str, str]] = []
        for rule in rules:
            kind = getattr(rule, "kind", None) if not isinstance(rule, Mapping) else rule.get("kind")
            key = getattr(rule, "key", None) if not isinstance(rule, Mapping) else rule.get("key")
            value = getattr(rule, "value", None) if not isinstance(rule, Mapping) else rule.get("value")
            if kind not in ALLOWED_KINDS:
                raise ValueError(f"unsupported overlay kind: {kind!r}")
            key_norm = normalize_key(str(key or ""))
            if not key_norm:
                raise ValueError("overlay rule key must not be empty")
            if not isinstance(value, Mapping):
                raise ValueError("overlay rule value must be an object")
            item_id = (rule.get("item_id") if isinstance(rule, Mapping) else None) or make_item_id(kind, key_norm)
            prepared.append((item_id, kind, key_norm, json.dumps(_thaw(value), ensure_ascii=False, separators=(",", ":"))))
        if not prepared:
            raise ValueError("no overlay rules to apply")

        def mutate(conn: sqlite3.Connection, _cur: int, new_version: int, _w: Dict[str, float]) -> None:
            now = utc_now_iso()
            for item_id, kind, key_norm, value_json in prepared:
                conn.execute(
                    "INSERT INTO overlay_items(item_id, kind, key_norm, value_json, active, config_version, updated_at) "
                    "VALUES (?,?,?,?,1,?,?) ON CONFLICT(item_id) DO UPDATE SET kind=excluded.kind, key_norm=excluded.key_norm, "
                    "value_json=excluded.value_json, active=1, config_version=excluded.config_version, updated_at=excluded.updated_at",
                    (item_id, kind, key_norm, value_json, new_version, now),
                )
            return None

        return self._commit(mutate, author=author, action=action, ticket_id=ticket_id, comment=comment)

    def update_weights(
        self,
        margin_weight: Optional[float] = None,
        promo_weight: Optional[float] = None,
        pop_weight: Optional[float] = None,
        *,
        extra: Optional[Mapping[str, float]] = None,
        author: str = "admin",
        comment: Optional[str] = None,
    ) -> OverlaySnapshot:
        """Горячее обновление весов (POST /v1/admin/ranking-weights): margin_weight -> margin_proxy,
        promo_weight -> promo, pop_weight -> popularity. `extra` — любые другие признаки по имени."""
        changes: Dict[str, Any] = {}
        for public, value in (("margin_weight", margin_weight), ("promo_weight", promo_weight), ("pop_weight", pop_weight)):
            if value is not None:
                changes[ADMIN_WEIGHT_ALIASES[public]] = value
        if extra:
            changes.update(extra)
        return self.update_weights_raw(changes, author=author, comment=comment)

    def update_weights_raw(
        self, changes: Mapping[str, Any], *, author: str = "admin", comment: Optional[str] = None
    ) -> OverlaySnapshot:
        cleaned = _clean_weights(changes)
        if not cleaned:
            raise ValueError("no weights to update")

        def mutate(_conn: sqlite3.Connection, _cur: int, _new: int, weights: Dict[str, float]) -> Dict[str, float]:
            weights.update(cleaned)
            return weights

        text = comment or "weights: " + ", ".join(f"{k}={v:g}" for k, v in sorted(cleaned.items()))
        return self._commit(mutate, author=author, action="manual_edit", comment=text)

    def rollback(
        self, target_version: Optional[int] = None, *, author: str = "admin", comment: Optional[str] = None
    ) -> OverlaySnapshot:
        """Откат к `target_version` (по умолчанию текущая - 1). Создаёт НОВУЮ версию с содержимым целевой."""

        def mutate(conn: sqlite3.Connection, current: int, new_version: int, _w: Dict[str, float]) -> Dict[str, float]:
            target = int(target_version) if target_version is not None else current - 1
            if target < 1:
                raise ValueError("nothing to roll back to")
            row = conn.execute(
                "SELECT overlay_snapshot_json, ranking_weights_json FROM config_versions WHERE config_version = ?", (target,)
            ).fetchone()
            if row is None:
                raise LookupError(f"config_version {target} not found")
            try:
                items = json.loads(row[0]) or []
            except (ValueError, TypeError) as exc:
                raise ValueError(f"config_version {target} has corrupted overlay snapshot") from exc
            now = utc_now_iso()
            conn.execute("UPDATE overlay_items SET active = 0, config_version = ?, updated_at = ? WHERE active = 1", (new_version, now))
            for it in items:
                conn.execute(
                    "INSERT INTO overlay_items(item_id, kind, key_norm, value_json, active, config_version, updated_at) "
                    "VALUES (?,?,?,?,1,?,?) ON CONFLICT(item_id) DO UPDATE SET kind=excluded.kind, key_norm=excluded.key_norm, "
                    "value_json=excluded.value_json, active=1, config_version=excluded.config_version, updated_at=excluded.updated_at",
                    (
                        it["item_id"], it["kind"], it["key_norm"],
                        json.dumps(it.get("value", {}), ensure_ascii=False, separators=(",", ":")),
                        new_version, now,
                    ),
                )
            return self._merge_weights(row[1])

        return self._commit(mutate, author=author, action="rollback", comment=comment or "rollback")

    # ------------------------------------------------------------------ аудит
    def list_versions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Метаданные версий (новые первыми) для GET /v1/admin/audit."""
        try:
            conn = connect_db(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT config_version, created_at, author, ticket_id, action, comment FROM config_versions "
                    "ORDER BY config_version DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.error("overlay list_versions failed: %s", exc)
            return []
        return [
            {"config_version": r[0], "created_at": r[1], "author": r[2], "ticket_id": r[3], "action": r[4], "comment": r[5]}
            for r in rows
        ]


_overlay_store: Optional[OverlayStore] = None
_overlay_store_lock = threading.Lock()


def get_overlay_store() -> OverlayStore:
    global _overlay_store
    with _overlay_store_lock:
        if _overlay_store is None:
            _overlay_store = OverlayStore()
        return _overlay_store
