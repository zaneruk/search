# защита от утечек: отсев совпадений с алиасами индекса (A.3)
"""Защита от утечек SYN-B (CONTRACTS.md A.3 п. 2):
исключает запрос из SYN-B, если его `query_norm` дословно совпадает с алиасом из индекса.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Set

from services.api.core.index_state import IndexState
from services.api.nlp.config import basic_norm


def collect_index_aliases(index_state: IndexState) -> Set[str]:
    aliases: Set[str] = set()
    if not index_state.is_loaded or index_state.entities is None:
        return aliases
    edf = index_state.entities
    for cname in edf["canonical_name"].dropna().astype(str).tolist():
        n = basic_norm(cname)
        if n:
            aliases.add(n)
    if "aliases_json" in edf.columns:
        for raw in edf["aliases_json"].dropna().astype(str).tolist():
            try:
                arr = json.loads(raw)
                if isinstance(arr, list):
                    for a in arr:
                        n = basic_norm(str(a))
                        if n:
                            aliases.add(n)
            except Exception:
                pass
    return aliases


def dedupe_queries_vs_index(items: List[Dict[str, Any]], index_state: IndexState) -> List[Dict[str, Any]]:
    aliases = collect_index_aliases(index_state)
    seen: Set[str] = set()
    kept: List[Dict[str, Any]] = []
    for item in items:
        qn = basic_norm(str(item.get("query") or ""))
        if not qn or qn in aliases or qn in seen:
            continue
        seen.add(qn)
        kept.append(item)
    return kept