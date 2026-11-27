"""L1: Entity / Alias lane (BRAND, FAMILY, CATEGORY, CONTENT, EXTERNAL_PRODUCT).

Возвращает:
  * ранжированный список family (`LaneHit`) из `target_family_ids` совпавших сущностей;
  * `nav_hits` (`CATEGORY`, `CONTENT`, `EXTERNAL_PRODUCT`) для блока навигации/баннера;
  * `out_of_catalog_banner`, если сработал `EXTERNAL_PRODUCT` (даже если в каталоге есть только продления).
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple, Dict

import pandas as pd

from services.api.app.schemas import NavHit
from services.api.core.index_state import IndexState
from services.api.nlp import NlpAnalysis, tokenize
from services.api.nlp.config import basic_norm
from services.api.retrieval.lanes import LaneHit

_KIND_PRIORITY = {
    "product_line": 1.0,
    "category": 0.78,
    "brand": 0.72,
    "product_type": 0.65,
    "external_product": 0.55,
    "content": 0.40,
    "other": 0.50,
}


def _clean_opt_str(val: Any) -> Optional[str]:
    if val is None or (not isinstance(val, (list, tuple, dict)) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return s


def _parse_str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x)]
    if pd.isna(raw):
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(x) for x in arr if str(x)]
            except ValueError:
                pass
        return [s] if s else []
    return []


def search_l1_entity(
    analysis: NlpAnalysis,
    index_state: IndexState,
    *,
    top_k: int = 50,
) -> Tuple[List[LaneHit], List[NavHit], Optional[str]]:
    if not index_state.is_loaded or index_state.families is None:
        return [], [], None

    lex = analysis.lexicon
    hits = list(analysis.qu.entity_hits)

    if not hits and analysis.hypotheses.applied_query:
        hits.extend(lex.find_entity_hits(tokenize(analysis.hypotheses.applied_query)))
    for h in analysis.hypotheses.hypotheses[:2]:
        if h.confidence >= 0.85:
            for eh in lex.find_entity_hits(tokenize(h.query)):
                if not any(x.ref.entity_id == eh.ref.entity_id for x in hits):
                    hits.append(eh)

    query_tokens_set = set(analysis.tokenized.lemmas) | set(analysis.tokenized.texts)
    families_df = index_state.families
    names_col = families_df["canonical_name"].fillna("").astype(str).tolist()
    avail_col = families_df["available_any"].fillna(False).astype(bool).tolist() if "available_any" in families_df.columns else [True] * len(families_df)
    pop_col = families_df["pop_prior"].fillna(0.0).astype(float).tolist() if "pop_prior" in families_df.columns else [0.0] * len(families_df)

    fam_scores: Dict[int, Tuple[float, List[str]]] = {}
    nav_hits: List[NavHit] = []
    seen_nav: set = set()
    has_direct_catalog_hit = False
    external_hits: List[Tuple[str, Optional[str]]] = []

    for eh in hits:
        ref = eh.ref
        ent_row = index_state.entity_row(ref.entity_id) or {}
        url = _clean_opt_str(ent_row.get("url"))
        snippet = _clean_opt_str(ent_row.get("evidence_snippet"))

        if ref.kind in ("category", "content", "external_product") and ref.entity_id not in seen_nav:
            seen_nav.add(ref.entity_id)
            nav_kind = "EXTERNAL_PRODUCT" if ref.kind == "external_product" else ref.kind.upper()
            nav_hits.append(
                NavHit(
                    entity_id=ref.entity_id,
                    kind=nav_kind,  # type: ignore[arg-type]
                    title=ref.canonical_name,
                    url=url,
                    note=snippet,
                )
            )
            if ref.kind == "external_product":
                external_hits.append((ref.canonical_name, snippet))

        if ref.kind in ("product_line", "brand", "category"):
            has_direct_catalog_hit = True

        target_fids = list(ref.family_ids) or _parse_str_list(ent_row.get("target_family_ids_json"))
        kind_w = _KIND_PRIORITY.get(ref.kind, 0.5)
        ent_conf = eh.confidence or 0.90
        field_tag = "aliases" if eh.via != "alias" else f"entity:{ref.kind}"

        for fid in target_fids:
            pos = index_state.family_index(fid)
            if pos is None:
                continue
            cname_norm = basic_norm(names_col[pos])
            tok_overlap = sum(1 for t in query_tokens_set if len(t) >= 2 and t in cname_norm)
            avail_bonus = 0.08 if avail_col[pos] else 0.0
            pop_bonus = min(0.15, float(pop_col[pos]) * 0.05)
            score = kind_w * ent_conf + 0.12 * tok_overlap + avail_bonus + pop_bonus
            prev = fam_scores.get(pos)
            if prev is None or score > prev[0]:
                fields = list(prev[1]) if prev else []
                if field_tag not in fields:
                    fields.append(field_tag)
                fam_scores[pos] = (score, fields)

    ranked = sorted(fam_scores.items(), key=lambda kv: (-kv[1][0], kv[0]))[:top_k]
    lane_hits = [
        LaneHit(
            family_idx=pos,
            family_id=index_state.family_id_at(pos),
            score=round(sc, 6),
            lane="l1_entity",
            matched_fields=tuple(fields),
        )
        for pos, (sc, fields) in ranked
    ]

    ooc_banner: Optional[str] = None
    if external_hits:
        ext_name, ext_note = external_hits[0]
        if not has_direct_catalog_hit:
            ooc_banner = ext_note or f"Продукт «{ext_name}» отсутствует в каталоге Softline. Показаны доступные аналоги и можно запросить поставку."
        else:
            ooc_banner = ext_note or f"Новые лицензии «{ext_name}» отсутствуют в каталоге; показаны доступные варианты и аналоги."

    return lane_hits, nav_hits[:5], ooc_banner