"""Учёт стоимости (A.12, B.6, B.10).

* Синхронизация `config/price_snapshots.json` -> таблица `price_snapshots`.
* Пересчёт токенов в целые микрорубли:
      cost_micro_rub = ((t_in - t_c) * p_in + t_c * p_c + t_out * p_out) * usd_to_rub
  где цены в USD за 1 млн токенов (1 токен * $/Mtok * курс = микрорубли).
  Если провайдер тарифицируется в рублях, задайте цены в ₽/Mtok и `usd_to_rub = 1.0`.
* Запись каждого вызова в `cost_ledger`.
* `CostMeterAccumulator` -> схема `CostMeter` для ответа поиска.

Допустимые форматы `config/price_snapshots.json` (ключи-синонимы перечислены в `_*_KEYS`):

    {
      "usd_to_rub": 92.5, "date": "2026-11-26", "source_url": "https://...",
      "models": {
        "aliceai-llm-flash": {"provider": "yandex", "usd_per_mtok_in": 0.1,
                              "usd_per_mtok_cached": 0.03, "usd_per_mtok_out": 0.3}
      }
    }

либо список объектов `{"model": ..., "provider": ..., ...}` (в корне или в `snapshots`).
Ключ модели может быть коротким именем или URI `gpt://<folder>/<model>/<ver>`.
Модель `"*"` задаёт цену по умолчанию. Для неизвестных моделей используется
служебный снимок `ps_unpriced` с нулевой стоимостью (чтобы запись в ledger не падала по FK).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from services.api.app.schemas import CostMeter, DataTier

log = logging.getLogger("smart_search.cost")

DEFAULT_DB_PATH = "data/db/smart_search.sqlite"
DEFAULT_PRICE_FILE = "config/price_snapshots.json"
UNPRICED_SNAPSHOT_ID = "ps_unpriced"
SECONDS_PER_MONTH = 2_592_000

_IN_KEYS = ("usd_per_mtok_in", "input_usd_per_mtok", "price_in", "input", "in")
_CACHED_KEYS = ("usd_per_mtok_cached", "cached_usd_per_mtok", "price_cached", "cached")
_OUT_KEYS = ("usd_per_mtok_out", "output_usd_per_mtok", "price_out", "output", "out")
_RATE_KEYS = ("usd_to_rub", "usd_rub", "rate")
_URL_KEYS = ("price_source_url", "source_url", "source", "url")
_DATE_KEYS = ("created_at", "date", "price_date")


# --------------------------------------------------------------------------- общие помощники БД/времени
def default_db_path() -> str:
    return (
        os.getenv("SMART_SEARCH_DB_PATH")
        or os.getenv("SQLITE_PATH")
        or DEFAULT_DB_PATH
    )

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_WAL_READY: set = set()
_WAL_LOCK = threading.Lock()


def connect_db(path: Optional[Union[str, Path]] = None, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Новое соединение SQLite (WAL, busy_timeout, foreign_keys). Закрывать должен вызывающий."""
    p = str(path or default_db_path())
    parent = os.path.dirname(os.path.abspath(p))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p, timeout=timeout)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    if p not in _WAL_READY:
        with _WAL_LOCK:
            conn.execute("PRAGMA journal_mode=WAL")
            _WAL_READY.add(p)
    return conn


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- цены
def normalize_model_name(model: str) -> str:
    """`gpt://<folder>/<model>/<version>` -> `<model>`; короткие имена возвращаются как есть."""
    m = (model or "").strip()
    if m.startswith("gpt://"):
        parts = m[len("gpt://"):].split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return m


@dataclass(frozen=True)
class PriceSnapshot:
    snapshot_id: str
    created_at: str
    provider: str
    model: str
    usd_per_mtok_in: float
    usd_per_mtok_cached: float
    usd_per_mtok_out: float
    usd_to_rub: float
    price_source_url: str


UNPRICED_SNAPSHOT = PriceSnapshot(
    snapshot_id=UNPRICED_SNAPSHOT_ID,
    created_at="1970-01-01T00:00:00Z",
    provider="unknown",
    model="*",
    usd_per_mtok_in=0.0,
    usd_per_mtok_cached=0.0,
    usd_per_mtok_out=0.0,
    usd_to_rub=1.0,
    price_source_url="unpriced",
)


def _first(d: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _to_rfc3339(value: Any) -> str:
    if not value:
        return utc_now_iso()
    s = str(value).strip()
    if len(s) == 10:
        return s + "T00:00:00Z"
    if s.endswith("Z"):
        return s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return utc_now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_snapshot_id(provider: str, model: str, p_in: float, p_c: float, p_out: float, rate: float, created_at: str, url: str) -> str:
    raw = f"{provider}|{model}|{p_in!r}|{p_c!r}|{p_out!r}|{rate!r}|{created_at}|{url}"
    return "ps_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def load_price_file(path: Union[str, Path]) -> List[PriceSnapshot]:
    """Читает файл тарифов. Некорректные записи пропускаются с предупреждением."""
    p = Path(path)
    if not p.is_file():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    top: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    container: Any = raw if isinstance(raw, list) else (raw.get("snapshots") or raw.get("models") or raw.get("prices"))

    entries: List[Tuple[Optional[str], Mapping[str, Any]]] = []
    if isinstance(container, dict):
        for name, entry in container.items():
            if isinstance(entry, dict):
                entries.append((str(name), entry))
    elif isinstance(container, list):
        for entry in container:
            if isinstance(entry, dict):
                entries.append((entry.get("model"), entry))

    snapshots: List[PriceSnapshot] = []
    for name, entry in entries:
        model = normalize_model_name(str(name or entry.get("model") or ""))
        if not model:
            continue
        try:
            provider = str(_first(entry, ("provider",), _first(top, ("provider",), "yandex"))).strip().lower()
            p_in = float(_first(entry, _IN_KEYS))
            p_out = float(_first(entry, _OUT_KEYS))
            p_c = float(_first(entry, _CACHED_KEYS, p_in))
            rate = float(_first(entry, _RATE_KEYS, _first(top, _RATE_KEYS)))
        except (TypeError, ValueError):
            log.warning("price_snapshots: skip %r: missing or invalid price/rate fields", model)
            continue
        if min(p_in, p_c, p_out) < 0 or rate <= 0 or not all(map(math.isfinite, (p_in, p_c, p_out, rate))):
            log.warning("price_snapshots: skip %r: negative or non-finite values", model)
            continue
        created_at = _to_rfc3339(_first(entry, _DATE_KEYS, _first(top, _DATE_KEYS)))
        url = str(_first(entry, _URL_KEYS, _first(top, _URL_KEYS, "unknown")))
        snapshots.append(
            PriceSnapshot(
                snapshot_id=_make_snapshot_id(provider, model, p_in, p_c, p_out, rate, created_at, url),
                created_at=created_at,
                provider=provider,
                model=model,
                usd_per_mtok_in=p_in,
                usd_per_mtok_cached=p_c,
                usd_per_mtok_out=p_out,
                usd_to_rub=rate,
                price_source_url=url,
            )
        )
    return snapshots


def compute_cost_micro_rub(price: PriceSnapshot, tokens_in: int, tokens_cached: int, tokens_out: int) -> int:
    """Стоимость вызова в целых микрорублях. `tokens_in` включает кэшированные токены."""
    t_in = max(0, int(tokens_in))
    t_c = min(max(0, int(tokens_cached)), t_in)
    t_out = max(0, int(tokens_out))
    micro = ((t_in - t_c) * price.usd_per_mtok_in + t_c * price.usd_per_mtok_cached + t_out * price.usd_per_mtok_out) * price.usd_to_rub
    if micro <= 0:
        return 0
    return int(math.ceil(micro - 1e-9))


_INSERT_SNAPSHOT_SQL = (
    "INSERT OR IGNORE INTO price_snapshots(snapshot_id, created_at, provider, model, usd_per_mtok_in, "
    "usd_per_mtok_cached, usd_per_mtok_out, usd_to_rub, price_source_url) VALUES (?,?,?,?,?,?,?,?,?)"
)


def _snapshot_params(s: PriceSnapshot) -> Tuple[Any, ...]:
    return (
        s.snapshot_id, s.created_at, s.provider, s.model, s.usd_per_mtok_in,
        s.usd_per_mtok_cached, s.usd_per_mtok_out, s.usd_to_rub, s.price_source_url,
    )


class PriceBook:
    """Кэш актуальных тарифов; источник истины — файл, история — таблица `price_snapshots`."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None, config_path: Optional[Union[str, Path]] = None) -> None:
        self._db_path = str(db_path or default_db_path())
        self._config_path = str(config_path or os.getenv("PRICE_SNAPSHOTS_PATH", DEFAULT_PRICE_FILE))
        self._lock = threading.RLock()
        self._index: Dict[Tuple[str, str], PriceSnapshot] = {("unknown", "*"): UNPRICED_SNAPSHOT}
        self._synced = False

    def sync(self) -> int:
        """Загружает тарифы из файла в БД (новые снимки), обновляет индекс. Возвращает число новых строк."""
        with self._lock:
            try:
                file_snaps = load_price_file(self._config_path)
            except (OSError, ValueError) as exc:
                log.error("price_snapshots: cannot read %s: %s", self._config_path, exc)
                file_snaps = []
            if not file_snaps:
                log.warning("price_snapshots: no tariffs loaded from %s; costs will be recorded as 0", self._config_path)

            inserted = 0
            all_snaps: List[PriceSnapshot] = [UNPRICED_SNAPSHOT]
            try:
                conn = connect_db(self._db_path)
                try:
                    with conn:
                        conn.execute(_INSERT_SNAPSHOT_SQL, _snapshot_params(UNPRICED_SNAPSHOT))
                        for s in file_snaps:
                            inserted += max(0, conn.execute(_INSERT_SNAPSHOT_SQL, _snapshot_params(s)).rowcount)
                    rows = conn.execute(
                        "SELECT snapshot_id, created_at, provider, model, usd_per_mtok_in, usd_per_mtok_cached, "
                        "usd_per_mtok_out, usd_to_rub, price_source_url FROM price_snapshots "
                        "ORDER BY created_at ASC, rowid ASC"
                    ).fetchall()
                finally:
                    conn.close()
                all_snaps = [PriceSnapshot(*r) for r in rows]
            except sqlite3.Error as exc:
                log.error("price_snapshots: DB sync failed: %s", exc)

            index: Dict[Tuple[str, str], PriceSnapshot] = {}
            for s in all_snaps:
                index[(s.provider, s.model)] = s
            for s in file_snaps:  # текущий файл приоритетнее истории в БД
                index[(s.provider, s.model)] = s
            index.setdefault(("unknown", "*"), UNPRICED_SNAPSHOT)
            self._index = index
            self._synced = True
            return inserted

    def lookup(self, provider: str, model: str) -> PriceSnapshot:
        if not self._synced:
            self.sync()
        name = normalize_model_name(model)
        prov = (provider or "").strip().lower()
        index = self._index
        snap = index.get((prov, name))
        if snap is None:
            snap = next((v for (_, m), v in index.items() if m == name), None)
        if snap is None:
            snap = next((v for (_, m), v in index.items() if m in ("*", "default") and v.snapshot_id != UNPRICED_SNAPSHOT_ID), None)
        return snap or UNPRICED_SNAPSHOT


_price_book: Optional[PriceBook] = None
_price_book_lock = threading.Lock()


def get_price_book() -> PriceBook:
    global _price_book
    with _price_book_lock:
        if _price_book is None:
            _price_book = PriceBook()
        return _price_book


def sync_price_snapshots(db_path: Optional[Union[str, Path]] = None, config_path: Optional[Union[str, Path]] = None) -> int:
    """Одноразовая синхронизация (например, в lifespan приложения). Обновляет и глобальный PriceBook."""
    book = PriceBook(db_path, config_path)
    inserted = book.sync()
    global _price_book
    with _price_book_lock:
        _price_book = book
    return inserted


# --------------------------------------------------------------------------- ledger
def record_llm_call(
    *,
    purpose: str,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    latency_ms: float,
    status: str,
    trace_id: Optional[str] = None,
    retry_n: int = 0,
    price_book: Optional[PriceBook] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """Считает стоимость и пишет строку в cost_ledger. Никогда не бросает исключений наружу.
    Возвращает cost_micro_rub (даже если запись в БД не удалась)."""
    book = price_book or get_price_book()
    price = book.lookup(provider, model)
    t_in = max(0, int(tokens_in))
    t_c = min(max(0, int(tokens_cached)), t_in)
    t_out = max(0, int(tokens_out))
    cost = compute_cost_micro_rub(price, t_in, t_c, t_out)
    try:
        conn = connect_db(db_path or book._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO cost_ledger(ts, purpose, provider, model, tokens_in, tokens_cached, tokens_out, "
                    "latency_ms, price_snapshot_id, cost_micro_rub, trace_id, status, retry_n) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        utc_now_iso(), purpose, (provider or "unknown").lower(), normalize_model_name(model),
                        t_in, t_c, t_out, round(float(latency_ms), 3), price.snapshot_id, cost,
                        trace_id, status, int(retry_n),
                    ),
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.error("cost_ledger write failed (purpose=%s model=%s): %s", purpose, model, exc)
    return cost


def daily_spend_micro_rub(db_path: Optional[Union[str, Path]] = None, day: Optional[str] = None) -> int:
    """Сумма cost_micro_rub в cost_ledger за указанные сутки UTC (по умолчанию сегодня). Бросает sqlite3.Error."""
    start = f"{day or utc_today()}T00:00:00Z"
    conn = connect_db(db_path)
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost_micro_rub), 0) FROM cost_ledger WHERE ts >= ?", (start,)).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


# --------------------------------------------------------------------------- CostMeter
def cpu_cost_micro_rub_per_sec() -> float:
    """c_cpu = Price_VPS_month / (vCPU * 2 592 000 с), в микрорублях за CPU-секунду.
    Берётся из `VPS_PRICE_RUB_MONTH` и `VPS_VCPU`; если цена не задана — 0 (CPU не тарифицируется)."""
    price = _env_float("VPS_PRICE_RUB_MONTH", 0.0)
    vcpu = _env_float("VPS_VCPU", 1.0)
    if price <= 0 or vcpu <= 0:
        return 0.0
    return price / (vcpu * SECONDS_PER_MONTH) * 1_000_000


class CostMeterAccumulator:
    """Накопитель стоимости одного запроса (потокобезопасный)."""

    def __init__(self, cpu_cost_per_sec_micro_rub: Optional[float] = None) -> None:
        self._lock = threading.Lock()
        self._c_cpu = cpu_cost_micro_rub_per_sec() if cpu_cost_per_sec_micro_rub is None else float(cpu_cost_per_sec_micro_rub)
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0
        self.llm_cost_micro_rub = 0
        self.cpu_ms_total = 0.0

    def add_llm_call(self, *, tokens_in: int = 0, tokens_cached: int = 0, tokens_out: int = 0, cost_micro_rub: int = 0, calls: int = 1) -> None:
        with self._lock:
            self.llm_calls += int(calls)
            self.tokens_in += int(tokens_in)
            self.tokens_cached += int(tokens_cached)
            self.tokens_out += int(tokens_out)
            self.llm_cost_micro_rub += int(cost_micro_rub)

    def add_cpu_ms(self, cpu_ms: float) -> None:
        with self._lock:
            self.cpu_ms_total += max(0.0, float(cpu_ms))

    def set_cpu_ms_total(self, cpu_ms: float) -> None:
        with self._lock:
            self.cpu_ms_total = max(0.0, float(cpu_ms))

    def build(self, data_tier: DataTier = DataTier.derived) -> CostMeter:
        with self._lock:
            cpu_cost = int(round(self.cpu_ms_total / 1000.0 * self._c_cpu))
            return CostMeter(
                llm_calls=self.llm_calls,
                tokens_in=self.tokens_in,
                tokens_cached=self.tokens_cached,
                tokens_out=self.tokens_out,
                cpu_ms_total=round(self.cpu_ms_total, 3),
                cost_micro_rub=self.llm_cost_micro_rub + cpu_cost,
                data_tier=data_tier,
            )
