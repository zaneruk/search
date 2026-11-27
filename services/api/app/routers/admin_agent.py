"""
Роутер Агента, Контента и Продукта (Экран 3):
- POST /v1/admin/agent/run
- GET  /v1/admin/tickets
- POST /v1/admin/tickets/{ticket_id}/apply
- POST /v1/admin/tickets/{ticket_id}/reject
- POST /v1/admin/overlay/rollback
- GET  /v1/admin/audit
- GET  /v1/admin/data-quality
- GET  /v1/admin/demand-gaps
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.api.app.routers.admin_deps import (
    get_artifacts_dir,
    get_current_config_version,
    get_db,
    load_default_ranking_weights,
    notify_overlay_reload,
    require_admin_token,
    utc_now_iso,
)

try:
    from services.api.app.schemas.admin import (
        ApplyTicketResponse,
        OverlayRulePayload,
        TicketItem,
    )
    from services.api.app.schemas.common import DataTier, RootCause, TicketType
except ImportError:
    from enum import Enum

    class DataTier(str, Enum):
        real = "real"
        derived = "derived"
        judged = "judged"
        simulated = "simulated"
        modeled = "modeled"

    class RootCause(str, Enum):
        R1 = "R1"
        R2 = "R2"
        R3 = "R3"
        R4 = "R4"
        R5 = "R5"
        R6 = "R6"
        R7 = "R7"
        R8 = "R8"
        R9 = "R9"
        R10 = "R10"

    class TicketType(str, Enum):
        ADD_SYNONYM = "ADD_SYNONYM"
        ADD_ALIAS = "ADD_ALIAS"
        CATALOG_GAP = "CATALOG_GAP"
        FIX_CARD = "FIX_CARD"
        RANKING_RULE = "RANKING_RULE"
        AVAILABILITY_ALERT = "AVAILABILITY_ALERT"
        INTENT_DISAMBIGUATION = "INTENT_DISAMBIGUATION"

    class OverlayRulePayload(BaseModel):
        kind: Literal["synonym", "alias", "pin", "demote", "boost", "catalog_patch"]
        key: str
        value: Dict[str, Any]

    class TicketItem(BaseModel):
        ticket_id: str
        ticket_type: TicketType
        root_cause: RootCause
        target_role: Literal["content", "product", "ecom"]
        cluster_key: str
        affected_queries: List[str]
        frequency: int
        estimated_monthly_rub: int
        estimation_tier: DataTier = DataTier.modeled
        confidence: float
        summary: str
        evidence: List[str] = Field(default_factory=list)
        proposed_patch: Optional[OverlayRulePayload] = None
        dry_run_before_hit4: Optional[float] = None
        dry_run_after_hit4: Optional[float] = None
        status: Literal["open", "applied", "rejected", "rolled_back"]
        applied_config_version: Optional[int] = None
        created_at: str

    class ApplyTicketResponse(BaseModel):
        ticket_id: str
        gold10_passed: bool
        gold10_details: List[Dict[str, Any]]
        new_config_version: int
        replay_results: List[Dict[str, Any]]


class ConfigVersionMeta(BaseModel):
    config_version: int
    created_at: str
    author: str
    ticket_id: Optional[str] = None
    action: str
    comment: Optional[str] = None
    overlay_items_count: int
    ranking_weights: Dict[str, float]


class RollbackIn(BaseModel):
    target_version: Optional[int] = None


class AgentRunIn(BaseModel):
    use_llm: bool = True
    max_clusters: int = Field(default=15, ge=1, le=50)


router = APIRouter(prefix="/v1/admin", tags=["admin-agent"], dependencies=[Depends(require_admin_token)])


def _norm_key(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("ё", "е").split())


def _make_item_id(kind: str, key_norm: str) -> str:
    digest = hashlib.sha1(f"{kind}:{key_norm}".encode("utf-8")).hexdigest()[:12]
    return f"ovl_{kind}_{digest}"


def _row_to_ticket(row) -> TicketItem:
    patch_raw = json.loads(row["proposed_patch_json"]) if row["proposed_patch_json"] else None
    patch_obj = OverlayRulePayload.model_validate(patch_raw) if patch_raw else None
    return TicketItem(
        ticket_id=str(row["ticket_id"]),
        ticket_type=TicketType(str(row["ticket_type"])),
        root_cause=RootCause(str(row["root_cause"])),
        target_role=str(row["target_role"]),  # type: ignore[arg-type]
        cluster_key=str(row["cluster_key"]),
        affected_queries=json.loads(row["affected_queries_json"]),
        frequency=int(row["frequency"]),
        estimated_monthly_rub=int(row["estimated_monthly_rub"]),
        estimation_tier=DataTier.modeled,
        confidence=float(row["confidence"]),
        summary=str(row["summary"]),
        evidence=json.loads(row["evidence_json"]),
        proposed_patch=patch_obj,
        dry_run_before_hit4=float(row["dry_run_before_hit4"]) if row["dry_run_before_hit4"] is not None else None,
        dry_run_after_hit4=float(row["dry_run_after_hit4"]) if row["dry_run_after_hit4"] is not None else None,
        status=str(row["status"]),  # type: ignore[arg-type]
        applied_config_version=int(row["applied_config_version"]) if row["applied_config_version"] is not None else None,
        created_at=str(row["created_at"]),
    )


def _load_gold10() -> List[Dict[str, str]]:
    gold_path = Path("eval/datasets/gold_map.csv")
    if not gold_path.exists():
        return []
    with gold_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _extract_hits_from_search_response(resp_obj: Any) -> Dict[str, Any]:
    """Достаёт family_ids, nav_entity_ids и top1 из Pydantic-модели или dict ответа Контура 1."""
    if hasattr(resp_obj, "model_dump"):
        data = resp_obj.model_dump()
    elif isinstance(resp_obj, dict):
        data = resp_obj
    else:
        return {"family_ids": [], "nav_ids": [], "top1_name": None}

    family_ids: List[str] = []
    top1_name: Optional[str] = None
    for sec in data.get("sections", []) or []:
        for item in sec.get("items", []) or []:
            fid = str(item.get("family_id") or "")
            if fid and fid not in family_ids:
                family_ids.append(fid)
            if top1_name is None:
                top1_name = item.get("canonical_name")

    nav_ids = [str(nh.get("entity_id") or "") for nh in (data.get("nav_hits", []) or [])]
    return {
        "family_ids": family_ids,
        "nav_ids": nav_ids,
        "top1_name": top1_name,
        "hard_null": bool(data.get("hard_null", False)),
        "soft_null": bool(data.get("soft_null", False)),
    }


def _run_gold10_check(
    request: Request,
    patch: Optional[Dict[str, Any]],
) -> tuple[bool, List[Dict[str, Any]]]:
    """
    Быстрая проверка регрессий на GOLD10 (A.8, B.8):
    - Если подключено поисковое ядро Контура 1 (app.state.probe_fn / search_engine) — прогоняет реальный поиск с temp_patch.
    - Иначе выполняет структурную проверку безопасности патча против gold_map.csv.
    """
    gold_rows = _load_gold10()
    if not gold_rows:
        return True, [{"status": "skipped", "reason": "eval/datasets/gold_map.csv не найден"}]

    probe_fn = getattr(request.app.state, "probe_fn", None)
    details: List[Dict[str, Any]] = []
    all_ok = True

    if callable(probe_fn):
        for g in gold_rows:
            q = g.get("query", "")
            kind = g.get("target_kind", "product")
            target_fid = g.get("target_family_id", "")
            target_eid = g.get("target_entity_id", "")
            try:
                # 1. Проверяем до патча (базовое состояние)
                res_before = probe_fn(q, None)
                parsed_before = _extract_hits_from_search_response(res_before)
                if kind in ("category", "content"):
                    ok_before = (target_eid in parsed_before["nav_ids"][:2]) or (target_fid in parsed_before["family_ids"][:4])
                else:
                    ok_before = target_fid in parsed_before["family_ids"][:4]

                # 2. Проверяем с кандидатным патчем
                res = probe_fn(q, patch)
                parsed = _extract_hits_from_search_response(res)
                if kind in ("category", "content"):
                    ok = (target_eid in parsed["nav_ids"][:2]) or (target_fid in parsed["family_ids"][:4])
                else:
                    ok = target_fid in parsed["family_ids"][:4]

                # Регрессия — это когда БЫЛО пройдено, а СТАЛО не пройдено
                if ok_before and not ok:
                    all_ok = False
                
                details.append(
                    {
                        "query": q,
                        "target_kind": kind,
                        "target_family_id": target_fid,
                        "passed": ok,
                        "regressed": (ok_before and not ok),
                        "top4_families": parsed["family_ids"][:4],
                        "mode": "live_probe",
                    }
                )
            except Exception as e:
                all_ok = False
                details.append({"query": q, "passed": False, "error": str(e), "mode": "live_probe"})
        return all_ok, details

    # Структурная проверка, когда роутер тестируется автономно до подключения Контура 1
    patch_kind = (patch or {}).get("kind")
    patch_key = _norm_key((patch or {}).get("key", ""))
    patch_val = (patch or {}).get("value") or {}
    demoted_ids = set(str(x) for x in (patch_val.get("family_ids") or [])) if patch_kind == "demote" else set()
    if patch_kind == "demote" and patch_val.get("family_id"):
        demoted_ids.add(str(patch_val["family_id"]))

    for g in gold_rows:
        q = g.get("query", "")
        q_norm = _norm_key(q)
        target_fid = g.get("target_family_id", "")
        blocked = False
        reason = "ok"

        if target_fid and target_fid in demoted_ids:
            blocked = True
            reason = f"Патч demote затрагивает целевую family {target_fid} из GOLD10"
        elif patch_kind in ("pin", "synonym") and patch_key == q_norm:
            pinned = str(patch_val.get("family_id") or "")
            if patch_kind == "pin" and pinned and pinned != target_fid:
                blocked = True
                reason = f"Патч pin перезаписывает точный запрос GOLD10 '{q}' на чужую family {pinned}"

        if blocked:
            all_ok = False
        details.append(
            {
                "query": q,
                "target_family_id": target_fid,
                "passed": not blocked,
                "reason": reason,
                "mode": "structural_fallback",
            }
        )
    return all_ok, details


def _run_replay_for_queries(request: Request, queries: List[str]) -> List[Dict[str, Any]]:
    probe_fn = getattr(request.app.state, "probe_fn", None)
    results: List[Dict[str, Any]] = []
    for q in queries[:10]:
        if callable(probe_fn):
            try:
                res = probe_fn(q, None)  # уже на обновлённом overlay после hot-swap!
                parsed = _extract_hits_from_search_response(res)
                results.append(
                    {
                        "query": q,
                        "replayed": True,
                        "hard_null": parsed["hard_null"],
                        "soft_null": parsed["soft_null"],
                        "top1_name": parsed["top1_name"],
                        "top4_families": parsed["family_ids"][:4],
                    }
                )
            except Exception as e:
                results.append({"query": q, "replayed": False, "error": str(e)})
        else:
            results.append(
                {
                    "query": q,
                    "replayed": False,
                    "note": "Overlay обновлён в SQLite; живой replay включится после подключения поискового ядра Контура 1",
                }
            )
    return results


@router.post("/agent/run")
def run_diagnostic_agent(body: Optional[AgentRunIn] = None) -> Dict[str, Any]:
    opts = body or AgentRunIn()
    try:
        from services.worker.jobs.agent_diagnose import run_agent_diagnose
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "WORKER_NOT_AVAILABLE",
                "message": f"Модуль агента диагностики ещё не добавлен ({e}). Будет готов в Части 4.",
                "trace_id": None,
                "details": None,
            },
        )
    result = run_agent_diagnose(use_llm=opts.use_llm, max_clusters=opts.max_clusters)
    return result


@router.get("/tickets", response_model=List[TicketItem])
def list_tickets(status: Optional[Literal["open", "applied", "rejected", "rolled_back"]] = Query(default=None)) -> List[TicketItem]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY estimated_monthly_rub DESC, created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, estimated_monthly_rub DESC, created_at DESC"
            ).fetchall()
    return [_row_to_ticket(r) for r in rows]


@router.post("/tickets/{ticket_id}/apply", response_model=ApplyTicketResponse)
def apply_ticket(ticket_id: str, request: Request) -> ApplyTicketResponse:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "TICKET_NOT_FOUND",
                    "message": f"Тикет {ticket_id} не найден",
                    "trace_id": None,
                    "details": None,
                },
            )
        if str(row["status"]) == "applied":
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "TICKET_ALREADY_APPLIED",
                    "message": f"Тикет {ticket_id} уже применён в версии {row['applied_config_version']}",
                    "trace_id": None,
                    "details": None,
                },
            )

        patch_dict = json.loads(row["proposed_patch_json"]) if row["proposed_patch_json"] else None
        cur_ver = get_current_config_version(conn)

        # Шаг 1: обязательная проверка GOLD10 перед фиксацией новой версии (A.8)
        gold_ok, gold_details = _run_gold10_check(request, patch_dict)
        if not gold_ok:
            return ApplyTicketResponse(
                ticket_id=ticket_id,
                gold10_passed=False,
                gold10_details=gold_details,
                new_config_version=cur_ver,
                replay_results=[],
            )

        # Шаг 2: запись патча в overlay_items и инкремент config_version
        new_ver = cur_ver + 1
        now = utc_now_iso()

        if patch_dict:
            kind = str(patch_dict["kind"])
            key_norm = _norm_key(str(patch_dict["key"]))
            val_json = json.dumps(patch_dict["value"], ensure_ascii=False)
            item_id = _make_item_id(kind, key_norm)
            conn.execute(
                """
                INSERT INTO overlay_items (item_id, kind, key_norm, value_json, active, config_version, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    value_json = excluded.value_json,
                    active = 1,
                    config_version = excluded.config_version,
                    updated_at = excluded.updated_at
                """,
                (item_id, kind, key_norm, val_json, new_ver, now),
            )

        active_items = [
            dict(r)
            for r in conn.execute(
                "SELECT item_id, kind, key_norm, value_json, config_version, updated_at FROM overlay_items WHERE active = 1"
            ).fetchall()
        ]
        prev_cfg = conn.execute(
            "SELECT ranking_weights_json FROM config_versions WHERE config_version = ?",
            (cur_ver,),
        ).fetchone()
        weights_json = prev_cfg["ranking_weights_json"] if prev_cfg else json.dumps(load_default_ranking_weights())

        conn.execute(
            """
            INSERT INTO config_versions (
                config_version, created_at, author, ticket_id,
                action, comment, overlay_snapshot_json, ranking_weights_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ver,
                now,
                "admin",
                ticket_id,
                "apply_ticket",
                f"Применение тикета {ticket_id}: {row['summary']}",
                json.dumps(active_items, ensure_ascii=False),
                weights_json,
            ),
        )
        conn.execute(
            "UPDATE tickets SET status = 'applied', applied_config_version = ? WHERE ticket_id = ?",
            (new_ver, ticket_id),
        )
        conn.execute(
            """
            INSERT INTO events (event_id, ts, event_type, session_id, trace_id, source, config_version, payload_json)
            VALUES (?, ?, 'admin_action', 'ss_admin', NULL, 'live', ?, ?)
            """,
            (
                f"evt_{hashlib.sha1(f'{ticket_id}:{now}'.encode()).hexdigest()[:16]}",
                now,
                new_ver,
                json.dumps({"action": "apply_ticket", "ticket_id": ticket_id, "new_config_version": new_ver}, ensure_ascii=False),
            ),
        )
        conn.commit()

        # Шаг 3: атомарный hot-swap ссылки OverlayState без рестарта
        notify_overlay_reload(request, conn)

        # Шаг 4: мгновенный replay пострадавших запросов
        affected_queries = json.loads(row["affected_queries_json"])

    replay = _run_replay_for_queries(request, affected_queries)
    return ApplyTicketResponse(
        ticket_id=ticket_id,
        gold10_passed=True,
        gold10_details=gold_details,
        new_config_version=new_ver,
        replay_results=replay,
    )


@router.post("/tickets/{ticket_id}/reject")
def reject_ticket(ticket_id: str) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.execute("UPDATE tickets SET status = 'rejected' WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "TICKET_NOT_FOUND", "message": f"Тикет {ticket_id} не найден", "trace_id": None, "details": None},
            )
    return {"ticket_id": ticket_id, "status": "rejected"}


@router.post("/overlay/rollback")
def rollback_overlay(request: Request, body: Optional[RollbackIn] = None) -> Dict[str, Any]:
    with get_db() as conn:
        cur_ver = get_current_config_version(conn)
        target_ver = body.target_version if (body and body.target_version is not None) else (cur_ver - 1)
        if target_ver < 1 or target_ver >= cur_ver:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "INVALID_ROLLBACK_VERSION",
                    "message": f"Нельзя откатиться на версию {target_ver} (текущая версия: {cur_ver})",
                    "trace_id": None,
                    "details": None,
                },
            )

        target_row = conn.execute(
            "SELECT overlay_snapshot_json, ranking_weights_json FROM config_versions WHERE config_version = ?",
            (target_ver,),
        ).fetchone()
        if target_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "VERSION_NOT_FOUND",
                    "message": f"Версия конфигурации {target_ver} не найдена",
                    "trace_id": None,
                    "details": None,
                },
            )

        snapshot = json.loads(target_row["overlay_snapshot_json"])
        weights_json = target_row["ranking_weights_json"]
        new_ver = cur_ver + 1
        now = utc_now_iso()

        # Восстанавливаем таблицу overlay_items строго по снимку target_version
        conn.execute("UPDATE overlay_items SET active = 0, updated_at = ?", (now,))
        for item in snapshot:
            conn.execute(
                """
                INSERT INTO overlay_items (item_id, kind, key_norm, value_json, active, config_version, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    value_json = excluded.value_json,
                    active = 1,
                    config_version = excluded.config_version,
                    updated_at = excluded.updated_at
                """,
                (
                    item["item_id"],
                    item["kind"],
                    item["key_norm"],
                    item["value_json"],
                    new_ver,
                    now,
                ),
            )

        conn.execute(
            """
            INSERT INTO config_versions (
                config_version, created_at, author, ticket_id,
                action, comment, overlay_snapshot_json, ranking_weights_json
            ) VALUES (?, ?, ?, NULL, 'rollback', ?, ?, ?)
            """,
            (
                new_ver,
                now,
                "admin",
                f"Откат конфигурации к версии {target_ver}",
                json.dumps(snapshot, ensure_ascii=False),
                weights_json,
            ),
        )
        # Помечаем тикет, применённый в откатную версию, как rolled_back
        conn.execute(
            "UPDATE tickets SET status = 'rolled_back' WHERE applied_config_version = ?",
            (cur_ver,),
        )
        conn.execute(
            """
            INSERT INTO events (event_id, ts, event_type, session_id, trace_id, source, config_version, payload_json)
            VALUES (?, ?, 'admin_action', 'ss_admin', NULL, 'live', ?, ?)
            """,
            (
                f"evt_{hashlib.sha1(f'rb:{new_ver}:{now}'.encode()).hexdigest()[:16]}",
                now,
                new_ver,
                json.dumps({"action": "rollback", "target_version": target_ver, "new_config_version": new_ver}, ensure_ascii=False),
            ),
        )
        conn.commit()
        notify_overlay_reload(request, conn)

    return {
        "status": "rolled_back",
        "rolled_back_to": target_ver,
        "new_config_version": new_ver,
        "restored_overlay_items": len(snapshot),
        "ranking_weights": json.loads(weights_json),
    }


@router.get("/audit", response_model=List[ConfigVersionMeta])
def get_audit_log() -> List[ConfigVersionMeta]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM config_versions ORDER BY config_version DESC LIMIT 100"
        ).fetchall()

    out: List[ConfigVersionMeta] = []
    for r in rows:
        snap = json.loads(r["overlay_snapshot_json"]) if r["overlay_snapshot_json"] else []
        weights = json.loads(r["ranking_weights_json"]) if r["ranking_weights_json"] else {}
        out.append(
            ConfigVersionMeta(
                config_version=int(r["config_version"]),
                created_at=str(r["created_at"]),
                author=str(r["author"]),
                ticket_id=r["ticket_id"],
                action=str(r["action"]),
                comment=r["comment"],
                overlay_items_count=len(snap),
                ranking_weights=weights,
            )
        )
    return out


@router.get("/data-quality")
def get_data_quality_issues(
    issue_type: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    art_dir = get_artifacts_dir()
    dqi_path = (art_dir / "data_quality_issues.parquet") if art_dir else Path("data/interim/data_quality_issues.parquet")
    fam_path = (art_dir / "families.parquet") if art_dir else Path("data/interim/families.parquet")

    if not dqi_path.exists():
        return {"total": 0, "items": [], "issue_type_counts": {}, "data_tier": "real"}

    df = pd.read_parquet(dqi_path)
    counts = {str(k): int(v) for k, v in df["issue_type"].value_counts().to_dict().items()} if len(df) > 0 else {}

    if issue_type:
        df = df[df["issue_type"] == issue_type]

    total = len(df)
    page_df = df.iloc[offset : offset + limit].copy()

    fam_meta: Dict[str, Dict[str, Any]] = {}
    if fam_path.exists() and len(page_df) > 0:
        fids = set(page_df["family_id"].astype(str).tolist())
        fdf = pd.read_parquet(fam_path, columns=["family_id", "canonical_name", "vendor", "data_quality_score"])
        fdf = fdf[fdf["family_id"].astype(str).isin(fids)]
        for _, fr in fdf.iterrows():
            fam_meta[str(fr["family_id"])] = {
                "canonical_name": str(fr["canonical_name"]),
                "vendor": str(fr["vendor"]),
                "data_quality_score": float(fr["data_quality_score"]),
            }

    items: List[Dict[str, Any]] = []
    for _, r in page_df.iterrows():
        fid = str(r["family_id"])
        meta = fam_meta.get(fid, {})
        items.append(
            {
                "family_id": fid,
                "offer_id": str(r.get("offer_id") or ""),
                "canonical_name": meta.get("canonical_name", ""),
                "vendor": meta.get("vendor", ""),
                "data_quality_score": meta.get("data_quality_score"),
                "field": str(r["field"]),
                "issue_type": str(r["issue_type"]),
                "details": str(r["details"]),
            }
        )

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "issue_type_counts": counts,
        "items": items,
        "data_tier": "real",
    }


@router.get("/demand-gaps")
def get_demand_gaps() -> Dict[str, Any]:
    art_dir = get_artifacts_dir()
    ent_path = (art_dir / "entities.parquet") if art_dir else None
    fam_path = (art_dir / "families.parquet") if art_dir else Path("data/interim/families.parquet")

    external_products: List[Dict[str, Any]] = []
    if ent_path and ent_path.exists():
        edf = pd.read_parquet(ent_path)
        ext = edf[edf["kind"] == "EXTERNAL_PRODUCT"]
        for _, er in ext.iterrows():
            external_products.append(
                {
                    "entity_id": str(er["entity_id"]),
                    "canonical_name": str(er["canonical_name"]),
                    "aliases": json.loads(er["aliases_json"]),
                    "url": er["url"] if pd.notna(er["url"]) else None,
                    "evidence_snippet": er["evidence_snippet"] if pd.notna(er["evidence_snippet"]) else None,
                    "data_tier": "real",
                }
            )

    unavailable_vendors: List[Dict[str, Any]] = []
    if fam_path.exists():
        fdf = pd.read_parquet(fam_path, columns=["family_id", "vendor", "available_any"])
        grp = (
            fdf.groupby("vendor", dropna=True)
            .agg(total_families=("family_id", "count"), available_families=("available_any", "sum"))
            .reset_index()
        )
        grp["unavailable_families"] = grp["total_families"] - grp["available_families"]
        grp["unavailable_share"] = (grp["unavailable_families"] / grp["total_families"]).round(4)
        top_unavail = grp[(grp["total_families"] >= 10) & (grp["unavailable_share"] >= 0.7)].sort_values(
            "unavailable_families", ascending=False
        ).head(12)
        for _, vr in top_unavail.iterrows():
            unavailable_vendors.append(
                {
                    "vendor": str(vr["vendor"]),
                    "total_families": int(vr["total_families"]),
                    "available_families": int(vr["available_families"]),
                    "unavailable_families": int(vr["unavailable_families"]),
                    "unavailable_share": float(vr["unavailable_share"]),
                    "data_tier": "real",
                }
            )

    with get_db() as conn:
        gap_tickets = [
            _row_to_ticket(r).model_dump()
            for r in conn.execute(
                "SELECT * FROM tickets WHERE root_cause IN ('R3', 'R4', 'R8') ORDER BY estimated_monthly_rub DESC"
            ).fetchall()
        ]
        lead_rows = conn.execute(
            "SELECT ts, session_id, source, payload_json FROM events WHERE event_type = 'catalog_gap_lead' ORDER BY ts DESC LIMIT 50"
        ).fetchall()
        leads = [
            {
                "ts": str(r["ts"]),
                "session_id": str(r["session_id"]),
                "source": str(r["source"]),
                **json.loads(r["payload_json"]),
            }
            for r in lead_rows
        ]
        null_clusters_rows = conn.execute(
            """
            SELECT query_norm, COUNT(*) AS cnt, MAX(hard_null) AS has_hard_null, MAX(soft_null) AS has_soft_null
            FROM search_traces
            WHERE hard_null = 1 OR soft_null = 1
            GROUP BY query_norm
            ORDER BY cnt DESC
            LIMIT 20
            """
        ).fetchall()
        null_queries = [dict(r) for r in null_clusters_rows]

    return {
        "external_products": external_products,
        "gap_tickets": gap_tickets,
        "catalog_gap_leads": leads,
        "top_null_queries": null_queries,
        "unavailable_top_vendors": unavailable_vendors,
        "data_tier": "derived",
    }