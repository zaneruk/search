"""Сборщик трассы шагов S0..S12 (B.4 TraceStep, B.9 SSE).

Использование:

    trace = TraceCollector(on_step=lambda s: queue.put_nowait(s))
    with trace.step(StepId.S4) as st:
        ...
        st.set_summary("Интент: hardware (0.72)")
        st.set_decision("rules_only")
        st.add_data(product_class_probs={"hardware": 0.72})
    # в async-коде:
    async with trace.astep(StepId.S5) as st: ...

`ms` — wall-clock (perf_counter), `cpu_ms` — CPU-время текущего потока (thread_time).
В async-коде CPU других задач того же потока между `await` попадает в замер — это
допустимая погрешность для оценки C_cpu.
Исключение внутри шага фиксируется как status="error" и пробрасывается дальше.
Ошибки callback-а логируются и никогда не ломают поиск.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, List, Optional, Set, Union

from services.api.app.schemas import StepId, TraceStep

log = logging.getLogger("smart_search.trace")

STEP_TITLES: Dict[str, str] = {
    "S0": "Приём и маскирование PII",
    "S1": "Нормализация, раскладка, гомоглифы",
    "S2": "Токенизация и лемматизация",
    "S3": "Гипотезы написания",
    "S4": "Понимание запроса (QU)",
    "S5": "Поиск L1–L5 и RRF-слияние",
    "S6": "Контекст и персонализация",
    "S7": "Ранжирование и overlay",
    "S8": "Выбор варианта (family → offer)",
    "S9": "Композиция и MMR",
    "S10": "Лестница гарантии ≥ 4 family",
    "S11": "Допродажи и ценовые бейджи",
    "S12": "Сборка ответа и запись события",
}

StepCallback = Callable[[TraceStep], Union[None, Awaitable[None]]]
_VALID_STATUS = ("ok", "skipped", "fallback", "error")


class StepHandle:
    """Изменяемое состояние шага внутри `with trace.step(...)`."""

    def __init__(self, step_id: StepId, title: str) -> None:
        self.step_id = step_id
        self.title = title
        self.status = "ok"
        self.summary = ""
        self.decision: Optional[str] = None
        self.fallback_used = False
        self.data: Dict[str, Any] = {}

    def set_summary(self, summary: str) -> "StepHandle":
        self.summary = str(summary)
        return self

    def set_decision(self, decision: Optional[str]) -> "StepHandle":
        self.decision = decision
        return self

    def set_status(self, status: str) -> "StepHandle":
        if status not in _VALID_STATUS:
            raise ValueError(f"invalid trace status: {status!r}")
        self.status = status
        return self

    def fallback(self, summary: Optional[str] = None, decision: Optional[str] = None) -> "StepHandle":
        self.status = "fallback"
        self.fallback_used = True
        if summary is not None:
            self.summary = summary
        if decision is not None:
            self.decision = decision
        return self

    def skip(self, summary: Optional[str] = None) -> "StepHandle":
        self.status = "skipped"
        if summary is not None:
            self.summary = summary
        return self

    def add_data(self, **kwargs: Any) -> "StepHandle":
        self.data.update(kwargs)
        return self

    def update_data(self, values: Dict[str, Any]) -> "StepHandle":
        self.data.update(values)
        return self


class TraceCollector:
    def __init__(self, on_step: Optional[StepCallback] = None, *, enabled: bool = True) -> None:
        self._on_step = on_step
        self.enabled = enabled
        self._steps: List[TraceStep] = []
        self._lock = threading.Lock()
        self._bg_tasks: Set["asyncio.Task[Any]"] = set()

    # ------------------------------------------------------------------ сборка шага
    def _finish(self, handle: StepHandle, t0: float, c0: float, exc: Optional[BaseException]) -> TraceStep:
        ms = max(0.0, (time.perf_counter() - t0) * 1000.0)
        cpu_ms = max(0.0, (time.thread_time() - c0) * 1000.0)
        status = handle.status
        summary = handle.summary
        if exc is not None:
            status = "error"
            if not summary:
                summary = f"{type(exc).__name__}: {exc}"[:200]
        elif handle.fallback_used and status == "ok":
            status = "fallback"
        step = TraceStep(
            id=handle.step_id,
            title=handle.title,
            status=status,
            ms=round(ms, 3),
            cpu_ms=round(cpu_ms, 3),
            summary=summary,
            decision=handle.decision,
            fallback_used=handle.fallback_used,
            data=handle.data,
        )
        with self._lock:
            self._steps.append(step)
        return step

    def _emit(self, step: TraceStep) -> Optional[Awaitable[None]]:
        """Вызывает callback. Возвращает awaitable, если callback асинхронный."""
        if self._on_step is None:
            return None
        try:
            result = self._on_step(step)
        except Exception:  # noqa: BLE001 - SSE не должен ломать поиск
            log.exception("trace callback failed")
            return None
        return result if inspect.isawaitable(result) else None

    def _schedule(self, awaitable: Awaitable[None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            return
        task = loop.create_task(self._safe_await(awaitable))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    async def _safe_await(awaitable: Awaitable[None]) -> None:
        try:
            await awaitable
        except Exception:  # noqa: BLE001
            log.exception("async trace callback failed")

    def _make_handle(self, step_id: Union[StepId, str], title: Optional[str]) -> StepHandle:
        sid = StepId(step_id)
        return StepHandle(sid, title or STEP_TITLES.get(sid.value, sid.value))

    # ------------------------------------------------------------------ context managers
    @contextmanager
    def step(self, step_id: Union[StepId, str], title: Optional[str] = None) -> Iterator[StepHandle]:
        handle = self._make_handle(step_id, title)
        t0, c0 = time.perf_counter(), time.thread_time()
        error: Optional[BaseException] = None
        try:
            yield handle
        except BaseException as exc:  # noqa: BLE001 - фиксируем и пробрасываем
            error = exc
            raise
        finally:
            step = self._finish(handle, t0, c0, error)
            pending = self._emit(step)
            if pending is not None:
                self._schedule(pending)

    @asynccontextmanager
    async def astep(self, step_id: Union[StepId, str], title: Optional[str] = None) -> AsyncIterator[StepHandle]:
        handle = self._make_handle(step_id, title)
        t0, c0 = time.perf_counter(), time.thread_time()
        error: Optional[BaseException] = None
        try:
            yield handle
        except BaseException as exc:  # noqa: BLE001
            error = exc
            raise
        finally:
            step = self._finish(handle, t0, c0, error)
            pending = self._emit(step)
            if pending is not None:
                try:
                    await pending
                except Exception:  # noqa: BLE001
                    log.exception("async trace callback failed")

    # ------------------------------------------------------------------ ручное добавление
    def record(
        self,
        step_id: Union[StepId, str],
        *,
        title: Optional[str] = None,
        status: str = "ok",
        ms: float = 0.0,
        cpu_ms: float = 0.0,
        summary: str = "",
        decision: Optional[str] = None,
        fallback_used: bool = False,
        data: Optional[Dict[str, Any]] = None,
    ) -> TraceStep:
        """Добавляет уже измеренный шаг (например, пропущенный или выполненный в другом потоке)."""
        if status not in _VALID_STATUS:
            raise ValueError(f"invalid trace status: {status!r}")
        sid = StepId(step_id)
        if fallback_used and status == "ok":
            status = "fallback"
        step = TraceStep(
            id=sid,
            title=title or STEP_TITLES.get(sid.value, sid.value),
            status=status,
            ms=round(max(0.0, float(ms)), 3),
            cpu_ms=round(max(0.0, float(cpu_ms)), 3),
            summary=summary,
            decision=decision,
            fallback_used=fallback_used,
            data=dict(data or {}),
        )
        with self._lock:
            self._steps.append(step)
        pending = self._emit(step)
        if pending is not None:
            self._schedule(pending)
        return step

    def skip(self, step_id: Union[StepId, str], summary: str = "", *, title: Optional[str] = None, decision: Optional[str] = None) -> TraceStep:
        return self.record(step_id, title=title, status="skipped", summary=summary, decision=decision)

    # ------------------------------------------------------------------ чтение
    @property
    def steps(self) -> List[TraceStep]:
        with self._lock:
            return list(self._steps)

    @property
    def cpu_ms_total(self) -> float:
        with self._lock:
            return round(sum(s.cpu_ms for s in self._steps), 3)

    @property
    def ms_total(self) -> float:
        with self._lock:
            return round(sum(s.ms for s in self._steps), 3)

    def find(self, step_id: Union[StepId, str]) -> Optional[TraceStep]:
        sid = StepId(step_id)
        with self._lock:
            for s in reversed(self._steps):
                if s.id == sid:
                    return s
        return None

    def to_response(self, include: bool = True) -> List[TraceStep]:
        """Список шагов для SearchResponse.trace (пустой, если `options.include_trace=False`)."""
        return self.steps if include else []


# --------------------------------------------------------------------------- SSE (B.9)
def format_sse(event: str, data: Any) -> str:
    """Формирует SSE-кадр. `data` — pydantic-модель, dict или строка JSON."""
    if hasattr(data, "model_dump"):
        payload = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    elif isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def format_sse_span(step: TraceStep) -> str:
    return format_sse("span", step)
