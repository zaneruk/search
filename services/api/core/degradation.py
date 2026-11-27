"""Режимы деградации L0..L3 (B.4 DegradationLevel, A.12, B.8 /v1/admin/chaos).

Уровни кумулятивные (каждый следующий включает ограничения предыдущего):
  L0 — полный пайплайн;
  L1 — без LLM (правила + кэш + dense): chaos force_disable_llm, открытый breaker LLM,
       исчерпан дневной бюджет `LLM_DAILY_BUDGET_MICRO_RUB` в cost_ledger;
  L2 — без dense-энкодера (и без LLM): chaos force_disable_dense, открытый breaker dense;
  L3 — базовый IndexState без overlay и БД: SQLite недоступна.

Бюджет: если `LLM_DAILY_BUDGET_MICRO_RUB` не задан, лимита нет; если задан, действует жёстко
(0 = LLM выключен). Сутки считаются по UTC, значение кэшируется на `budget_cache_ttl_s` секунд
и увеличивается сразу при `record_spend()`. Chaos-флаги живут только в памяти процесса.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from services.api.app.schemas import DegradationLevel
from services.api.core.cost_meter import connect_db, daily_spend_micro_rub, default_db_path, utc_today
from services.api.llm.breaker import BreakerState, CircuitBreaker, get_dense_breaker, get_llm_breaker

log = logging.getLogger("smart_search.degradation")

_UNSET: Any = object()


def _env_budget() -> Optional[int]:
    raw = os.getenv("LLM_DAILY_BUDGET_MICRO_RUB", "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        log.error("invalid LLM_DAILY_BUDGET_MICRO_RUB=%r, budget limit ignored", raw)
        return None


class DegradationManager:
    def __init__(
        self,
        *,
        db_path: Optional[Union[str, Path]] = None,
        llm_breaker: Optional[CircuitBreaker] = None,
        dense_breaker: Optional[CircuitBreaker] = None,
        daily_budget_micro_rub: Any = _UNSET,
        budget_cache_ttl_s: float = 5.0,
        db_cooldown_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = str(db_path or default_db_path())
        self.llm_breaker = llm_breaker or get_llm_breaker()
        self.dense_breaker = dense_breaker or get_dense_breaker()
        self._budget: Optional[int] = _env_budget() if daily_budget_micro_rub is _UNSET else daily_budget_micro_rub
        self._budget_ttl_s = float(budget_cache_ttl_s)
        self._db_cooldown_s = float(db_cooldown_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._chaos_llm = False
        self._chaos_dense = False
        self._chaos_overlay = False
        self._db_down_until = 0.0
        self._spent = 0
        self._spent_day = ""
        self._spent_checked_at = -1e18

    # ------------------------------------------------------------------ chaos
    # В __init__ добавить: self._chaos_overlay = False
    def set_chaos(
        self,
        force_disable_llm: Optional[bool] = None,
        force_disable_dense: Optional[bool] = None,
        force_disable_overlay: Optional[bool] = None,
    ) -> DegradationLevel:
        with self._lock:
            if force_disable_llm is not None:
                self._chaos_llm = bool(force_disable_llm)
            if force_disable_dense is not None:
                self._chaos_dense = bool(force_disable_dense)
            if force_disable_overlay is not None:
                self._chaos_overlay = bool(force_disable_overlay)
        return self.level()

    @property
    def force_disable_llm(self) -> bool:
        return self._chaos_llm

    @property
    def force_disable_dense(self) -> bool:
        return self._chaos_dense

    @property
    def force_disable_overlay(self) -> bool:
        return self._chaos_overlay

    def current_level(self) -> DegradationLevel:
        return self.level()

    def chaos_flags(self) -> Dict[str, bool]:
        with self._lock:
            return {"force_disable_llm": self._chaos_llm, "force_disable_dense": self._chaos_dense}

    def clear_chaos(self) -> DegradationLevel:
        return self.set_chaos(False, False,False)

    # ------------------------------------------------------------------ состояние БД
    def mark_db_failure(self) -> None:
        with self._lock:
            self._db_down_until = self._clock() + self._db_cooldown_s

    def mark_db_ok(self) -> None:
        with self._lock:
            self._db_down_until = 0.0

    @property
    def db_down(self) -> bool:
        with self._lock:
            return self._clock() < self._db_down_until

    def probe_db(self) -> bool:
        """Проверяет SQLite (`SELECT 1`) и обновляет флаг недоступности БД."""
        try:
            conn = connect_db(self._db_path)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.error("DB probe failed: %s", exc)
            self.mark_db_failure()
            return False
        self.mark_db_ok()
        return True

    # ------------------------------------------------------------------ бюджет
    @property
    def daily_budget_micro_rub(self) -> Optional[int]:
        return self._budget

    def set_daily_budget(self, micro_rub: Optional[int]) -> None:
        with self._lock:
            self._budget = micro_rub

    def spent_today_micro_rub(self) -> int:
        with self._lock:
            now = self._clock()
            today = utc_today()
            if self._spent_day != today or now - self._spent_checked_at >= self._budget_ttl_s:
                try:
                    self._spent = daily_spend_micro_rub(self._db_path, today)
                    self._spent_day = today
                    self._spent_checked_at = now
                except sqlite3.Error as exc:
                    log.error("cost_ledger read failed: %s", exc)
                    self._db_down_until = now + self._db_cooldown_s
                    self._spent_checked_at = now
                    if self._spent_day != today:
                        self._spent, self._spent_day = 0, today
            return self._spent

    def record_spend(self, micro_rub: int) -> None:
        """Мгновенно учитывает расход (вызывает gateway после записи в cost_ledger)."""
        with self._lock:
            if self._spent_day == utc_today():
                self._spent += max(0, int(micro_rub))

    def budget_exceeded(self) -> bool:
        if self._budget is None:
            return False
        return self.spent_today_micro_rub() >= self._budget

    # ------------------------------------------------------------------ вычисление уровня
    def _evaluate(self) -> Tuple[DegradationLevel, List[str]]:
        if self.db_down or self._chaos_overlay: return DegradationLevel.L3, ["db_unavailable" if self.db_down else "chaos:force_disable_overlay"]
        if self.db_down:
            return DegradationLevel.L3, ["db_unavailable"]
        with self._lock:
            chaos_llm, chaos_dense = self._chaos_llm, self._chaos_dense
        dense_reasons: List[str] = []
        if chaos_dense:
            dense_reasons.append("chaos:force_disable_dense")
        if self.dense_breaker.state is BreakerState.OPEN:
            dense_reasons.append("dense_breaker_open")
        llm_reasons: List[str] = []
        if chaos_llm:
            llm_reasons.append("chaos:force_disable_llm")
        if self.llm_breaker.state is BreakerState.OPEN:
            llm_reasons.append("llm_breaker_open")
        if self.budget_exceeded():
            llm_reasons.append("daily_budget_exceeded")
        if self.db_down:  # бюджет мог обнаружить недоступность БД
            return DegradationLevel.L3, ["db_unavailable"]
        if dense_reasons:
            return DegradationLevel.L2, dense_reasons + llm_reasons
        if llm_reasons:
            return DegradationLevel.L1, llm_reasons
        return DegradationLevel.L0, []

    def level(self) -> DegradationLevel:
        return self._evaluate()[0]

    def reasons(self) -> List[str]:
        return self._evaluate()[1]

    # ------------------------------------------------------------------ решения для пайплайна
    def llm_allowed(self) -> bool:
        return self.level() is DegradationLevel.L0

    def allow_llm(self, requested: bool = True) -> bool:
        """Можно ли вызывать LLM с учётом `options.llm_enabled` и текущего уровня."""
        return bool(requested) and self.llm_allowed()

    def dense_allowed(self) -> bool:
        return self.level() in (DegradationLevel.L0, DegradationLevel.L1)

    def allow_dense(self, disable_requested: bool = False) -> bool:
        """Можно ли использовать dense с учётом `options.disable_dense` и текущего уровня."""
        return (not disable_requested) and self.dense_allowed()

    def overlay_allowed(self) -> bool:
        return self.level() is not DegradationLevel.L3

    def db_allowed(self) -> bool:
        return self.level() is not DegradationLevel.L3

    def dense_call_allowed(self) -> bool:
        """Резервирует слот вызова dense-энкодера через breaker (после allow_dense). Результат — record_dense()."""
        with self._lock:
            if self._chaos_dense:
                return False
        return self.dense_breaker.allow()

    def record_dense(self, ok: bool) -> None:
        if ok:
            self.dense_breaker.record_success()
        else:
            self.dense_breaker.record_failure()

    # ------------------------------------------------------------------ для /v1/admin/ops и /readyz
    def snapshot(self) -> Dict[str, Any]:
        level, reasons = self._evaluate()
        budget = self._budget
        spent = self.spent_today_micro_rub() if budget is not None else None
        return {
            "degradation_level": level.value,
            "reasons": reasons,
            "chaos": self.chaos_flags(),
            "db_down": self.db_down,
            "llm_breaker": self.llm_breaker.snapshot(),
            "dense_breaker": self.dense_breaker.snapshot(),
            "budget": {
                "limit_micro_rub": budget,
                "spent_micro_rub": spent,
                "remaining_micro_rub": None if budget is None or spent is None else max(0, budget - spent),
                "exceeded": bool(budget is not None and spent is not None and spent >= budget),
            },
        }


_manager: Optional[DegradationManager] = None
_manager_lock = threading.Lock()


def get_degradation_manager() -> DegradationManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DegradationManager()
        return _manager
