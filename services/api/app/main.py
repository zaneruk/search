"""Временная заглушка API (Волна 0).

Что делает:
  * при старте накатывает contracts/sql/001_init.sql в SQLITE_PATH (скрипт идемпотентен:
    все CREATE ... IF NOT EXISTS);
  * отдаёт GET /healthz и GET /readyz по B.8;
  * регистрирует публичные ручки (/v1/suggest, /v1/search, /v1/search/stream, /v1/events)
    с моделями из app.schemas, чтобы /openapi.json уже содержал контракт для typegen.
    Реализации нет: ручки отвечают 501 с телом ApiError;
  * соблюдает B.3: ответ всегда несёт X-Trace-Id, X-Config-Version, X-Degradation-Level,
    а любые ошибки 4xx/5xx имеют форму ApiError.

Когда появятся routers/public.py и routers/internal.py, ручки-заглушки отсюда удаляются,
а роутеры подключаются через app.include_router(...).
"""

import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from ulid import ULID

from app.schemas.admin import EventIn
from app.schemas.common import ApiError, DegradationLevel, Segment
from app.schemas.search import SearchRequest, SearchResponse, SuggestResponse

log = logging.getLogger("smart_search.api")

CONTRACT_VERSION = "2.0.0-mvp"
# services/api/app/main.py -> parents[3] == корень репозитория (и /app в контейнере)
REPO_ROOT = Path(__file__).resolve().parents[3]


class StubSettings(BaseSettings):
    """Минимальные настройки заглушки. Позже их заменит app/settings.py."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sqlite_path: str = "data/db/smart_search.sqlite"
    contracts_sql_path: str = str(REPO_ROOT / "contracts" / "sql" / "001_init.sql")
    artifacts_dir: str = "data/artifacts"


# --------------------------------------------------------------------------- модели


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    index_version: Optional[str] = None
    config_version: int
    degradation: DegradationLevel


class EventAccepted(BaseModel):
    event_id: str


# --------------------------------------------------------------------------- БД и индекс


def apply_migrations(db_path: Path, sql_path: Path) -> None:
    """Накатывает единую SQL-схему (B.6) в SQLite; безопасно вызывать повторно."""
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


def read_config_version(db_path: Path) -> int:
    """Текущая config_version = максимум из config_versions (0, пока таблица пуста)."""
    conn = sqlite3.connect(str(db_path), timeout=3)
    try:
        row = conn.execute("SELECT COALESCE(MAX(config_version), 0) FROM config_versions").fetchone()
        return int(row[0])
    finally:
        conn.close()


def read_index_version(artifacts_dir: Path) -> Optional[str]:
    """index_version из data/artifacts/current (симлинк на каталог или файл-указатель, B.5)."""
    current = artifacts_dir / "current"
    try:
        if current.is_dir():
            manifest = current / "manifest.json"
            if manifest.is_file():
                value = json.loads(manifest.read_text(encoding="utf-8")).get("index_version")
                return str(value) if value else None
        elif current.is_file():
            pointer = current.read_text(encoding="utf-8").strip()
            manifest = artifacts_dir / pointer / "manifest.json"
            if manifest.is_file():
                value = json.loads(manifest.read_text(encoding="utf-8")).get("index_version")
                return str(value) if value else pointer
            return pointer or None
    except (OSError, ValueError):
        log.warning("Не удалось прочитать manifest.json индекса", exc_info=True)
    return None


# --------------------------------------------------------------------------- lifespan


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO)
    settings = StubSettings()
    db_path = Path(settings.sqlite_path)
    apply_migrations(db_path, Path(settings.contracts_sql_path))
    log.info("SQLite готова: %s (схема %s)", db_path, settings.contracts_sql_path)

    app.state.settings = settings
    app.state.db_path = db_path
    app.state.artifacts_dir = Path(settings.artifacts_dir)
    app.state.config_version = read_config_version(db_path)
    app.state.index_version = read_index_version(app.state.artifacts_dir)
    app.state.degradation = DegradationLevel.L0
    yield


# --------------------------------------------------------------------------- middleware


class ContractHeadersMiddleware:
    """B.3: X-Trace-Id, X-Config-Version, X-Degradation-Level в каждом ответе.

    Чистый ASGI-middleware (не BaseHTTPMiddleware), чтобы не буферизовать SSE.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = f"tr_{ULID()}"
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state = scope["app"].state
                headers = MutableHeaders(scope=message)
                headers["X-Trace-Id"] = trace_id
                headers["X-Config-Version"] = str(getattr(state, "config_version", 0))
                degradation = getattr(state, "degradation", DegradationLevel.L0)
                headers["X-Degradation-Level"] = degradation.value
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- приложение

app = FastAPI(
    title="smart-search API",
    version=CONTRACT_VERSION,
    description="Умный поиск для store.softline.ru. Контракт: CONTRACTS.md (B.8).",
    lifespan=lifespan,
)
app.add_middleware(ContractHeadersMiddleware)


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


def not_implemented(request: Request, endpoint: str) -> JSONResponse:
    return error_response(
        request,
        501,
        "not_implemented",
        f"{endpoint}: реализация появится после Волны 0",
    )


# --------------------------------------------------------------------------- health

NOT_IMPLEMENTED_DOC = {501: {"model": ApiError, "description": "Заглушка Волны 0"}}


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
    state = request.app.state
    try:
        config_version = read_config_version(state.db_path)
    except sqlite3.Error as exc:
        log.error("readyz: SQLite недоступна: %s", exc)
        return error_response(request, 503, "db_unavailable", "SQLite недоступна")

    state.config_version = config_version
    # До первой сборки индекса index_version = null (B.3: отсутствующее значение — null).
    state.index_version = read_index_version(state.artifacts_dir)
    return ReadyResponse(
        index_version=state.index_version,
        config_version=config_version,
        degradation=state.degradation,
    )


# --------------------------------------------------------------------------- public (заглушки)


@app.get(
    "/v1/suggest",
    response_model=SuggestResponse,
    responses=NOT_IMPLEMENTED_DOC,
    tags=["public"],
    summary="Typeahead",
)
def suggest(
    request: Request,
    q: str = Query(..., max_length=100),
    segment: Segment = Segment.smb,
) -> Any:
    return not_implemented(request, "GET /v1/suggest")


@app.post(
    "/v1/search",
    response_model=SearchResponse,
    responses=NOT_IMPLEMENTED_DOC,
    tags=["public"],
    summary="Полный поиск (S0–S12)",
)
def search(request: Request, body: SearchRequest) -> Any:
    return not_implemented(request, "POST /v1/search")


@app.post(
    "/v1/search/stream",
    responses={
        200: {
            "description": "SSE: события span, final, error (B.9)",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **NOT_IMPLEMENTED_DOC,
    },
    tags=["public"],
    summary="Поиск с потоком шагов trace по SSE",
)
def search_stream(request: Request, body: SearchRequest) -> Any:
    return not_implemented(request, "POST /v1/search/stream")


@app.post(
    "/v1/events",
    status_code=202,
    response_model=EventAccepted,
    responses=NOT_IMPLEMENTED_DOC,
    tags=["public"],
    summary="Логирование кликов, корзины, 👎, catalog_gap_lead",
)
def events(request: Request, body: EventIn) -> Any:
    return not_implemented(request, "POST /v1/events")
