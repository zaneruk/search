"""Публичные эндпоинты Витрины (CONTRACTS.md B.8, B.9)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.api.app.schemas import EventIn, SearchRequest, SearchResponse, Segment, SuggestResponse
from services.api.core.degradation import get_degradation_manager
from services.api.core.events import get_event_store
from services.api.core.index_state import get_index_state
from services.api.core.overlay import get_overlay_store
from services.api.pipeline import execute_search, execute_search_stream
from services.api.suggest import build_suggest

router = APIRouter(tags=["public"])

REPO_ROOT = Path(__file__).resolve().parents[4]


class EventAccepted(BaseModel):
    event_id: str


def _read_config_json(filename: str) -> Any:
    for base in (Path("config"), REPO_ROOT / "config"):
        p = base / filename
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return []


@router.get("/v1/suggest", response_model=SuggestResponse, summary="Typeahead")
def suggest(
    q: str = Query("", max_length=100),
    segment: Segment = Segment.smb,
) -> SuggestResponse:
    idx = get_index_state()
    ov = get_overlay_store()
    ov.maybe_refresh()
    return build_suggest(q, segment=segment, index_state=idx, overlay=ov.snapshot())


@router.post("/v1/search", response_model=SearchResponse, summary="Полный поиск (S0–S12)")
async def search(request: Request, body: SearchRequest, response: Response) -> SearchResponse:
    tid = getattr(request.state, "trace_id", None)
    res = await execute_search(body, trace_id=tid, source="live")
    response.headers["X-Trace-Id"] = res.trace_id
    response.headers["X-Config-Version"] = str(res.config_version)
    response.headers["X-Degradation-Level"] = res.degradation_level.value
    return res


@router.post(
    "/v1/search/stream",
    responses={
        200: {
            "description": "SSE: события span, final, error (B.9)",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
    summary="Поиск с потоком шагов trace по SSE",
)
async def search_stream(request: Request, body: SearchRequest) -> StreamingResponse:
    tid = getattr(request.state, "trace_id", None)
    return StreamingResponse(
        execute_search_stream(body, trace_id=tid, source="live"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/events", status_code=202, response_model=EventAccepted, summary="Логирование событий")
async def events(body: EventIn) -> EventAccepted:
    ov = get_overlay_store()
    eid = await get_event_store().awrite_event_in(body, config_version=ov.snapshot().config_version)
    return EventAccepted(event_id=eid)


@router.get("/v1/personas", summary="Список демо-персон")
def personas() -> Any:
    return _read_config_json("personas.json")


@router.get("/v1/scenarios", summary="Сценарные кнопки")
def scenarios() -> Any:
    return _read_config_json("demo_scenarios.json")