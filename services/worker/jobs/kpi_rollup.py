"""
Агрегация и снэпшот текущих KPI в data/interim/kpi_rollup.json (для мониторинга воркера).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from services.api.app.routers.admin_analytics import _compute_kpi_for_source
from services.api.app.routers.admin_deps import get_db, utc_now_iso


def run_kpi_rollup(out_path: str = "data/interim/kpi_rollup.json") -> Dict[str, Any]:
    with get_db() as conn:
        live = _compute_kpi_for_source(conn, "live")
        sim = _compute_kpi_for_source(conn, "simulated")
        total_tickets = int(conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'").fetchone()[0])

    payload = {
        "updated_at": utc_now_iso(),
        "open_tickets": total_tickets,
        "live": live,
        "simulated": sim,
    }
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run_kpi_rollup(), ensure_ascii=False, indent=2))