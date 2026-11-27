"""Точка входа FastAPI (CONTRACTS.md B.3, B.8)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.api.app.routers import internal, public
from services.api.app.schemas.common import ApiError, DegradationLevel
from services.api.core.cache import get_qu_cache
from services.api.core.cost_meter import default_db_path, sync_price_snapshots
from services.api.core.degradation import get_degradation_manager
from services.api.core.events import new_trace_id
from services.api.core.index_state import get_index_state
from services.api.core.overlay import get_overlay_store
from services.api.llm.gateway import get_gateway

log = logging.getLogger("smart_search.api")

CONTRACT_VERSION = "2.0.0-mvp"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dotenv_defaults() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv_defaults()


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    index_version: Optional[str] = None
    config_version: int
    degradation: DegradationLevel


def apply_migrations(db_path: Path, sql_path: Path) -> None:
    if not sql_path.is_file():
        raise FileNotFoundError(f"SQL-схема не найдена: {sql_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    script = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO)
    _load_dotenv_defaults()

    # 1. Накат contracts/sql/001_init.sql
    db_path = Path(default_db_path())
    sql_path = Path(os.getenv("CONTRACTS_SQL_PATH", str(REPO_ROOT / "contracts" / "sql" / "001_init.sql")))
    apply_migrations(db_path, sql_path)

    # 2. Синхронизация тарифов LLM
    await asyncio.to_thread(sync_price_snapshots)

    # 3. Загрузка OverlayStore и индекса
    idx = get_index_state()
    ov = get_overlay_store()
    ov.load()
    deg = get_degradation_manager()

    # 4. Старт LLMGateway (прогрев HTTP/TLS + price_book) и прогрев QU-кэша
    gw = get_gateway()
    await gw.startup()
    get_qu_cache().warm_from_db(limit=512)

    app.state.db_path = db_path
    app.state.index_state = idx
    app.state.overlay = ov
    app.state.degradation = deg
    app.state.index_version = idx.index_version if idx.is_loaded else None
    app.state.config_version = ov.snapshot().config_version
    log.info(
        "API готово: index=%s config_version=%d deg=%s",
        app.state.index_version,
        app.state.config_version,
        deg.level().value,
    )
    try:
        yield
    finally:
        await gw.aclose()


class ContractHeadersMiddleware:
    """B.3: X-Trace-Id, X-Config-Version, X-Degradation-Level в каждом ответе (совместимо с SSE)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = new_trace_id()
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "x-trace-id" not in headers:
                    headers["X-Trace-Id"] = trace_id
                if "x-config-version" not in headers:
                    headers["X-Config-Version"] = str(get_overlay_store().snapshot().config_version)
                if "x-degradation-level" not in headers:
                    headers["X-Degradation-Level"] = get_degradation_manager().level().value
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(
    title="smart-search API",
    version=CONTRACT_VERSION,
    description="Умный поиск для store.softline.ru. Контракт: CONTRACTS.md (B.8).",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id", "X-Config-Version", "X-Degradation-Level"],
)
app.add_middleware(ContractHeadersMiddleware)
app.include_router(public.router)
app.include_router(internal.router)

# Безопасное автоподключение админ-роутеров Контура 3 (после слияния с веткой напарника)
for _mod_name in ("admin_analytics", "admin_agent", "admin_ops"):
    try:
        _mod = __import__(f"services.api.app.routers.{_mod_name}", fromlist=["router"])
        app.include_router(_mod.router)
    except ImportError:
        pass


def error_response(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    body = ApiError(
        error_code=error_code,
        message=message,
        trace_id=getattr(request.state, "trace_id", None),
        details=details,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers=headers)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error_code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return error_response(request, exc.status_code, f"http_{exc.status_code}", message, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        request,
        422,
        "validation_error",
        "Некорректный запрос",
        details={"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    return error_response(request, 500, "internal_error", "Внутренняя ошибка сервера")


@app.get("/healthz", response_model=HealthResponse, tags=["health"], summary="Liveness-проба")
def healthz() -> HealthResponse:
    return HealthResponse()


@app.get(
    "/readyz",
    response_model=ReadyResponse,
    responses={503: {"model": ApiError, "description": "БД недоступна"}},
    tags=["health"],
    summary="Готовность индекса и БД",
)
def readyz(request: Request) -> Any:
    deg = get_degradation_manager()
    if not deg.probe_db():
        return error_response(request, 503, "db_unavailable", "SQLite недоступна")
    idx = get_index_state()
    ov = get_overlay_store()
    return ReadyResponse(
        index_version=idx.index_version if idx.is_loaded else None,
        config_version=ov.snapshot().config_version,
        degradation=deg.level(),
    )