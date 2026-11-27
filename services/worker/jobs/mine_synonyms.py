"""
Майнер кандидатов синонимов из цепочек переформулировок в events (п. 4.3):
Ищет внутри одной сессии переходы:
  search_issued(q1) -> [refine] -> search_issued(q2) -> result_click / add_to_cart / order
Считает частоту (support) и возвращает кандидатов с evidence.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from services.api.app.routers.admin_deps import get_db
from services.worker.agent.tools import norm_str


def mine_synonym_candidates(
    conn: Optional[sqlite3.Connection] = None,
    min_support: int = 2,
) -> List[Dict[str, Any]]:
    close_after = False
    if conn is None:
        cm = get_db()
        conn = cm.__enter__()
        close_after = True

    try:
        rows = conn.execute(
            """
            SELECT ts, session_id, event_type, trace_id, payload_json
            FROM events
            WHERE event_type IN ('search_issued', 'refine', 'result_click', 'add_to_cart', 'order')
            ORDER BY session_id, ts ASC
            """
        ).fetchall()

        by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            try:
                pl = json.loads(r["payload_json"])
            except Exception:
                pl = {}
            by_session[str(r["session_id"])].append(
                {
                    "ts": str(r["ts"]),
                    "event_type": str(r["event_type"]),
                    "trace_id": r["trace_id"],
                    "payload": pl,
                }
            )

        pair_counts: Counter = Counter()
        pair_examples: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

        for sid, evs in by_session.items():
            # Собираем последовательность (q, clicked_family_id) внутри сессии
            searches: List[Tuple[str, Optional[str], bool]] = []
            # track last trace_id -> index in searches
            trace_to_idx: Dict[str, int] = {}

            for ev in evs:
                et = ev["event_type"]
                pl = ev["payload"]
                if et == "search_issued":
                    q = norm_str(pl.get("query", ""))
                    if q:
                        searches.append((q, None, False))
                        if ev["trace_id"]:
                            trace_to_idx[str(ev["trace_id"])] = len(searches) - 1
                elif et == "refine":
                    q_from = norm_str(pl.get("from_query", ""))
                    q_to = norm_str(pl.get("to_query", ""))
                    if q_from and q_to and q_from != q_to:
                        # Явное событие переформулировки
                        pair_counts[(q_from, q_to)] += 1
                        pair_examples[(q_from, q_to)].append({"session_id": sid, "via": "refine_event"})
                elif et in ("result_click", "add_to_cart", "order"):
                    tid = str(ev["trace_id"] or "")
                    if tid in trace_to_idx:
                        idx = trace_to_idx[tid]
                        q_val, _, _ = searches[idx]
                        fid = pl.get("family_id")
                        searches[idx] = (q_val, str(fid) if fid else None, True)

            # Ищем переходы q_prev (без клика) -> q_next (с кликом) в окне до 2 шагов
            for i in range(len(searches) - 1):
                q1, _, clicked1 = searches[i]
                if clicked1:
                    continue
                for j in (i + 1, i + 2):
                    if j >= len(searches):
                        break
                    q2, fid2, clicked2 = searches[j]
                    if clicked2 and q1 != q2 and len(q1) >= 3 and len(q2) >= 3:
                        pair_counts[(q1, q2)] += 1
                        pair_examples[(q1, q2)].append({"session_id": sid, "clicked_family_id": fid2, "via": "session_sequence"})
                        break

        candidates: List[Dict[str, Any]] = []
        for (q1, q2), sup in pair_counts.most_common():
            if sup < min_support:
                continue
            exs = pair_examples[(q1, q2)]
            fids = [e["clicked_family_id"] for e in exs if e.get("clicked_family_id")]
            candidates.append(
                {
                    "q1_norm": q1,
                    "q2_norm": q2,
                    "support": int(sup),
                    "clicked_family_ids": list(dict.fromkeys(fids))[:5],
                    "evidence": f"Переход '{q1}' -> '{q2}' встретился {sup} раз(а) с последующим кликом",
                }
            )
        return candidates
    finally:
        if close_after:
            cm.__exit__(None, None, None)


if __name__ == "__main__":
    cands = mine_synonym_candidates(min_support=1)
    print(json.dumps(cands[:15], ensure_ascii=False, indent=2))