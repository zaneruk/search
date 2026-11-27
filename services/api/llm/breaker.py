"""Потокобезопасный circuit breaker (CLOSED -> OPEN -> HALF_OPEN -> CLOSED).

Семантика:
* CLOSED: вызовы разрешены. Открывается, когда подряд накоплено `failure_threshold`
  неудач внутри окна `window_s` (любой успех сбрасывает счётчик).
* OPEN: вызовы запрещены `cooldown_s` секунд.
* HALF_OPEN: после cooldown пропускается не более `half_open_max_calls` пробных вызовов.
  Успех пробы закрывает breaker, неудача открывает его заново с новым cooldown.

Контракт использования: каждый `allow() == True` обязан завершиться ровно одним из
`record_success()`, `record_failure()` или `record_cancelled()`.

Параметры по умолчанию читаются из окружения (префикс задаётся в `from_env`):
`<PREFIX>_FAILURES` (5), `<PREFIX>_WINDOW_S` (30), `<PREFIX>_COOLDOWN_S` (30).
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from enum import Enum
from typing import Any, Callable, Deque, Dict, Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class CircuitBreaker:
    def __init__(
        self,
        name: str = "llm",
        *,
        failure_threshold: int = 5,
        window_s: float = 30.0,
        cooldown_s: float = 30.0,
        half_open_max_calls: int = 1,
        probe_timeout_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_s < 0 or window_s <= 0:
            raise ValueError("window_s must be > 0 and cooldown_s >= 0")
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.window_s = float(window_s)
        self.cooldown_s = float(cooldown_s)
        self.half_open_max_calls = max(1, int(half_open_max_calls))
        # если проба "потерялась" (нет record_*), через это время слот освобождается
        self.probe_timeout_s = float(probe_timeout_s) if probe_timeout_s is not None else max(5.0, cooldown_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._state = BreakerState.CLOSED
        self._failures: Deque[float] = deque()
        self._opened_at = 0.0
        self._probes_in_flight = 0
        self._probe_started_at = 0.0
        self._opened_count = 0
        self._total_success = 0
        self._total_failure = 0
        self._rejected = 0

    @classmethod
    def from_env(cls, name: str = "llm", prefix: str = "LLM_BREAKER", **defaults: Any) -> "CircuitBreaker":
        """Значения из `<PREFIX>_FAILURES/_WINDOW_S/_COOLDOWN_S`; `defaults` действуют, если переменная не задана."""
        params: Dict[str, Any] = {
            "failure_threshold": int(_env_float(f"{prefix}_FAILURES", float(defaults.pop("failure_threshold", 5)))),
            "window_s": _env_float(f"{prefix}_WINDOW_S", float(defaults.pop("window_s", 30.0))),
            "cooldown_s": _env_float(f"{prefix}_COOLDOWN_S", float(defaults.pop("cooldown_s", 30.0))),
        }
        params.update(defaults)
        return cls(name, **params)

    # ------------------------------------------------------------------ internals
    def _refresh_locked(self, now: float) -> None:
        if self._state is BreakerState.OPEN and now - self._opened_at >= self.cooldown_s:
            self._state = BreakerState.HALF_OPEN
            self._probes_in_flight = 0
        elif (
            self._state is BreakerState.HALF_OPEN
            and self._probes_in_flight > 0
            and now - self._probe_started_at >= self.probe_timeout_s
        ):
            self._probes_in_flight = 0

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _open_locked(self, now: float) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = now
        self._probes_in_flight = 0
        self._opened_count += 1

    # ------------------------------------------------------------------ public API
    def allow(self) -> bool:
        """Можно ли сделать вызов. В HALF_OPEN резервирует слот пробы."""
        with self._lock:
            now = self._clock()
            self._refresh_locked(now)
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                self._rejected += 1
                return False
            if self._probes_in_flight < self.half_open_max_calls:
                self._probes_in_flight += 1
                self._probe_started_at = now
                return True
            self._rejected += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            self._total_success += 1
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.CLOSED
                self._probes_in_flight = 0
            if self._state is BreakerState.CLOSED:
                self._failures.clear()

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._total_failure += 1
            if self._state is BreakerState.HALF_OPEN:
                self._failures.clear()
                self._open_locked(now)
                return
            if self._state is BreakerState.OPEN:
                return  # запоздавшая неудача уже открытого breaker
            self._failures.append(now)
            self._prune_locked(now)
            if len(self._failures) >= self.failure_threshold:
                self._failures.clear()
                self._open_locked(now)

    def record_cancelled(self) -> None:
        """Вызов отменён, исход неизвестен: освобождаем слот пробы без учёта в статистике."""
        with self._lock:
            if self._state is BreakerState.HALF_OPEN and self._probes_in_flight > 0:
                self._probes_in_flight -= 1

    # ------------------------------------------------------------------ introspection
    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._refresh_locked(self._clock())
            return self._state

    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    def retry_in_s(self) -> float:
        with self._lock:
            now = self._clock()
            self._refresh_locked(now)
            if self._state is not BreakerState.OPEN:
                return 0.0
            return max(0.0, self.cooldown_s - (now - self._opened_at))

    def trip(self) -> None:
        """Принудительно открыть breaker (для chaos-тестов)."""
        with self._lock:
            self._failures.clear()
            self._open_locked(self._clock())

    def reset(self) -> None:
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failures.clear()
            self._probes_in_flight = 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._refresh_locked(now)
            self._prune_locked(now)
            return {
                "name": self.name,
                "state": self._state.value,
                "failures_in_window": len(self._failures),
                "failure_threshold": self.failure_threshold,
                "window_s": self.window_s,
                "cooldown_s": self.cooldown_s,
                "retry_in_s": round(
                    max(0.0, self.cooldown_s - (now - self._opened_at)) if self._state is BreakerState.OPEN else 0.0, 3
                ),
                "opened_count": self._opened_count,
                "total_success": self._total_success,
                "total_failure": self._total_failure,
                "rejected": self._rejected,
            }


_singleton_lock = threading.Lock()
_llm_breaker: Optional[CircuitBreaker] = None
_dense_breaker: Optional[CircuitBreaker] = None


def get_llm_breaker() -> CircuitBreaker:
    global _llm_breaker
    with _singleton_lock:
        if _llm_breaker is None:
            _llm_breaker = CircuitBreaker.from_env("llm", "LLM_BREAKER")
        return _llm_breaker


def get_dense_breaker() -> CircuitBreaker:
    global _dense_breaker
    with _singleton_lock:
        if _dense_breaker is None:
            _dense_breaker = CircuitBreaker.from_env("dense", "DENSE_BREAKER", failure_threshold=3, cooldown_s=20.0)
        return _dense_breaker
