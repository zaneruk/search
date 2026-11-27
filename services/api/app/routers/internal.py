"""Внутренние эндпоинты для диагностического агента (CONTRACTS.md B.8)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from services.api.app.schemas import OverlayRulePayload, SearchResponse
from services.api.pipeline import run_probe

router = APIRouter(tags=["internal"])


class ProbeRequest(BaseModel):
    query: str = Field(..., max_length=300)
    temp_patch: Optional[Union[OverlayRulePayload, List[OverlayRulePayload], Dict[str, Any]]] = None
    options: Optional[Dict[str, Any]] = None


def require_internal_token(x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token")) -> str:
    expected = os.getenv("INTERNAL_TOKEN", "change-me-internal").strip()
    if not expected:
        return "ok"
    if not x_internal_token or x_internal_token.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "UNAUTHORIZED",
                "message": "Неверный или отсутствующий заголовок X-Internal-Token",
                "trace_id": None,
                "details": None,
            },
        )
    return x_internal_token


@router.post("/internal/probe", response_model=SearchResponse, summary="Dry-run прогон с временным overlay-патчем")
async def probe(body: ProbeRequest, _token: str = Header(default=None, alias="X-Internal-Token")) -> SearchResponse:
    require_internal_token(_token)
    return await run_probe(body.query, body.temp_patch, req_options=body.options)