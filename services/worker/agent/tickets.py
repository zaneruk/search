"""
Управление тикетами диагностического агента в SQLite (B.4.3, B.6).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional

from services.api.app.routers.admin_deps import utc_now_iso


def estimate_monthly_rub(
    frequency: int,
    avg_order_rub: float = 15000.0,
    cvr_s: float = 0.075,
    kappa: float = 0.30,
    scale_to_month: float = 30.0,
) -> int:
    """
    Оценка потенциально теряемой выручки в месяц (tier=modeled) по формуле A.10,
    приведённая к месячному масштабу.
    """
    monthly_sessions = max(1, frequency) * scale_to_month
    lost_orders = monthly_sessions * cvr_s * (1.0 - kappa)
    return int(round(lost_orders * avg_order_rub))


def make_ticket_id(cluster_key: str, root_cause: str) -> str:
    digest = hashlib.sha1(f"{cluster_key}:{root_cause}".encode("utf-8")).hexdigest()[:14].upper()
    return f"tkt_{digest}"


def upsert_ticket(
    conn: sqlite3.Connection,
    ticket_type: str,
    root_cause: str,
    target_role: str,
    cluster_key: str,
    affected_queries: List[str],
    frequency: int,
    estimated_monthly_rub: int,
    confidence: float,
    summary: str,
    evidence: List[str],
    proposed_patch: Optional[Dict[str, Any]],
    dry_run_before_hit4: Optional[float],
    dry_run_after_hit4: Optional[float],
) -> Dict[str, Any]:
    """
    Создаёт новый тикет или обновляет существующий 'open' тикет с тем же cluster_key.
    Возвращает {'ticket_id': ..., 'created': bool}.
    """
    existing = conn.execute(
        "SELECT ticket_id, affected_queries_json, frequency FROM tickets WHERE cluster_key = ? AND status = 'open'",
        (cluster_key,),
    ).fetchone()

    now = utc_now_iso()
    patch_json = json.dumps(proposed_patch, ensure_ascii=False) if proposed_patch else None

    if existing is not None:
        tid = str(existing["ticket_id"])
        old_queries = json.loads(existing["affected_queries_json"])
        merged_queries = list(dict.fromkeys(old_queries + affected_queries))[:20]
        new_freq = max(int(existing["frequency"]), int(frequency))
        conn.execute(
            """
            UPDATE tickets SET
                ticket_type = ?,
                root_cause = ?,
                target_role = ?,
                affected_queries_json = ?,
                frequency = ?,
                estimated_monthly_rub = ?,
                confidence = ?,
                summary = ?,
                evidence_json = ?,
                proposed_patch_json = ?,
                dry_run_before_hit4 = ?,
                dry_run_after_hit4 = ?
            WHERE ticket_id = ?
            """,
            (
                ticket_type,
                root_cause,
                target_role,
                json.dumps(merged_queries, ensure_ascii=False),
                new_freq,
                int(estimated_monthly_rub),
                round(float(confidence), 3),
                summary,
                json.dumps(evidence[:10], ensure_ascii=False),
                patch_json,
                dry_run_before_hit4,
                dry_run_after_hit4,
                tid,
            ),
        )
        return {"ticket_id": tid, "created": False}

    tid = make_ticket_id(cluster_key, f"{root_cause}:{now}")
    conn.execute(
        """
        INSERT INTO tickets (
            ticket_id, created_at, ticket_type, root_cause, target_role,
            cluster_key, affected_queries_json, frequency, estimated_monthly_rub,
            confidence, summary, evidence_json, proposed_patch_json,
            dry_run_before_hit4, dry_run_after_hit4, status, applied_config_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL)
        """,
        (
            tid,
            now,
            ticket_type,
            root_cause,
            target_role,
            cluster_key,
            json.dumps(affected_queries[:20], ensure_ascii=False),
            int(frequency),
            int(estimated_monthly_rub),
            round(float(confidence), 3),
            summary,
            json.dumps(evidence[:10], ensure_ascii=False),
            patch_json,
            dry_run_before_hit4,
            dry_run_after_hit4,
        ),
    )
    return {"ticket_id": tid, "created": True}