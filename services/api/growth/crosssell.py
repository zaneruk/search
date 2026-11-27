"""S11: Допродажи (complements, upsell_to, analog_of) по graph_edges.parquet.

Инвариант I6: не выдаёт `available=false`, `on_request` как основное предложение,
`purchased_family_ids` пользователя и дубли из основной выдачи.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd

from services.api.app.schemas import ClientContext, CrossSellItem
from services.api.core.index_state import IndexState

_SOURCE_PRIORITY = {
    "vendor_evidence": 0,
    "catalog_structure": 1,
    "llm_validated": 2,
    "co_click": 3,
}


def _clean_opt(val: Any) -> Optional[str]:
    if val is None or (not isinstance(val, (list, tuple, dict)) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return s


def build_cross_sell(
    anchor_family_ids: Sequence[str],
    shown_family_ids: Set[str],
    ctx: ClientContext,
    index_state: IndexState,
    *,
    max_items: int = 6,
    per_relation_max: int = 3,
    min_confidence: float = 0.6,
    allow_analog: bool = False,
) -> List[CrossSellItem]:
    if not index_state.is_loaded or index_state.graph_edges is None or index_state.families is None:
        return []
    edges_df = index_state.graph_edges
    if len(edges_df) == 0 or not anchor_family_ids:
        return []

    excluded: Set[str] = set(shown_family_ids) | set(ctx.purchased_family_ids)
    anchors = list(anchor_family_ids[:5])

    sub = edges_df[edges_df["src_family_id"].astype(str).isin(anchors)]
    if len(sub) == 0:
        return []

    allowed_rels = {"complements", "upsell_to", "requires", "integrates_with"}
    if allow_analog:
        allowed_rels.add("analog_of")

    candidates: List[Tuple[int, float, str, CrossSellItem]] = []
    seen_dst: Set[str] = set()

    for row in sub.to_dict("records"):
        dst_fid = _clean_opt(row.get("dst_family_id"))
        if not dst_fid or dst_fid in excluded or dst_fid in seen_dst:
            continue
        rel = _clean_opt(row.get("edge_type")) or "complements"
        if rel not in allowed_rels:
            continue
        conf = float(row.get("confidence") or 0.0) if not pd.isna(row.get("confidence")) else 0.0
        if conf < min_confidence:
            continue

        fam = index_state.family_row(dst_fid)
        if fam is None:
            continue

        avail = bool(fam.get("available_any")) if not pd.isna(fam.get("available_any")) else False
        pmin_raw = fam.get("price_min_minor")
        pmin = int(float(pmin_raw)) if pmin_raw is not None and not pd.isna(pmin_raw) else None

        # I6: только в наличии и с ценой (не on_request)
        if not avail or pmin is None or pmin <= 0:
            continue

        src_kind = _clean_opt(row.get("source")) or "catalog_structure"
        prio = _SOURCE_PRIORITY.get(src_kind, 4)
        ev_label = _clean_opt(row.get("snippet"))
        ev_url = _clean_opt(row.get("evidence_url"))
        pop = float(fam.get("pop_prior") or 0.0) if not pd.isna(fam.get("pop_prior")) else 0.0

        seen_dst.add(dst_fid)
        item = CrossSellItem(
            family_id=dst_fid,
            canonical_name=str(fam.get("canonical_name") or ""),
            vendor=str(fam.get("vendor") or ""),
            relation_type="upsell_to" if rel == "upsell_to" else ("analog_of" if rel == "analog_of" else "complements"),
            source=src_kind,
            evidence_label=ev_label,
            evidence_url=ev_url,
            price_min_minor=pmin,
            available=True,
        )
        candidates.append((prio, -(conf + pop * 0.1), dst_fid, item))

    candidates.sort(key=lambda t: (t[0], t[1], t[2]))

    out: List[CrossSellItem] = []
    rel_counts: Dict[str, int] = {}
    for _, _, _, item in candidates:
        cnt = rel_counts.get(item.relation_type, 0)
        if cnt >= per_relation_max:
            continue
        rel_counts[item.relation_type] = cnt + 1
        out.append(item)
        if len(out) >= max_items:
            break
    return out