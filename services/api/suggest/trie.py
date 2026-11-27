"""Быстрый префиксный поиск поверх `typeahead_trie.pkl` (формат build.py)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from services.api.app.schemas import SuggestCompletion, SuggestProductHit
from services.api.core.index_state import IndexState
from services.api.nlp.config import basic_norm


@dataclass(frozen=True)
class TrieLookupResult:
    completions: List[SuggestCompletion]
    products: List[SuggestProductHit]


_fast_lock = threading.Lock()
_fast_ver: Optional[str] = None
_fast_pool: List[Tuple[str, str, Mapping[str, Any]]] = []


def _get_fast_pool(index_state: IndexState, families: List[Mapping[str, Any]]) -> List[Tuple[str, str, Mapping[str, Any]]]:
    global _fast_ver, _fast_pool
    with _fast_lock:
        if _fast_ver == index_state.index_version and _fast_pool:
            return _fast_pool
    top = sorted(
        families,
        key=lambda f: (bool(f.get("available")), float(f.get("pop_prior") or 0.0)),
        reverse=True,
    )[:2500]
    built = [
        (str(f.get("canonical_name") or "").lower(), str(f.get("vendor") or "").lower(), f)
        for f in top
    ]
    with _fast_lock:
        _fast_ver = index_state.index_version
        _fast_pool = built
    return built


def _to_product_hit(f: Mapping[str, Any]) -> SuggestProductHit:
    pmin = f.get("price_min_minor")
    price = int(pmin) if pmin is not None and int(pmin) > 0 else None
    img = f.get("image_url")
    image_url = str(img) if img is not None and str(img).lower() not in ("", "nan", "none") else None
    return SuggestProductHit(
        family_id=str(f.get("family_id") or ""),
        canonical_name=str(f.get("canonical_name") or ""),
        vendor=str(f.get("vendor") or ""),
        image_url=image_url,
        price_min_minor=price,
        available=bool(f.get("available")),
        has_promo=bool(f.get("has_promo")),
    )


def lookup_trie(
    prefix_norm: str,
    index_state: IndexState,
    *,
    max_completions: int = 8,
    max_products: int = 4,
) -> TrieLookupResult:
    trie = index_state.typeahead_trie
    if not isinstance(trie, Mapping):
        return TrieLookupResult(completions=[], products=[])

    families: List[Mapping[str, Any]] = trie.get("families") or []
    completions_data: List[Mapping[str, Any]] = trie.get("completions") or []
    prefix_map: Mapping[str, List[int]] = trie.get("prefix_map") or {}

    if not prefix_norm:
        fast_pool = _get_fast_pool(index_state, families)
        top_comps = [
            SuggestCompletion(
                text=str(c["text"]),
                kind=c.get("kind", "product"),
                score=float(c.get("score") or 0.0),
            )
            for c in completions_data[:max_completions]
            if c.get("kind") in ("brand", "product", "alias", "category", "seed_query", "layout_fix")
        ]
        return TrieLookupResult(completions=top_comps, products=[_to_product_hit(f) for _, _, f in fast_pool[:max_products]])

    key = prefix_norm[:8]
    cand_indices = list(prefix_map.get(key, []))
    if not cand_indices and " " in prefix_norm:
        last_word = prefix_norm.split()[-1][:8]
        if len(last_word) >= 2:
            cand_indices = list(prefix_map.get(last_word, []))

    matched_comps: List[SuggestCompletion] = []
    prod_hits: List[SuggestProductHit] = []
    seen_fids: set = set()
    seen_texts: set = set()

    for cid in cand_indices:
        if cid < 0 or cid >= len(completions_data):
            continue
        c = completions_data[cid]
        tnorm = str(c.get("text_norm") or basic_norm(str(c.get("text") or "")))
        if len(prefix_norm) > 8 and prefix_norm not in tnorm:
            continue
        txt = str(c["text"])
        if txt.lower() not in seen_texts and len(matched_comps) < max_completions:
            seen_texts.add(txt.lower())
            kind = str(c.get("kind") or "product")
            if kind not in ("brand", "product", "alias", "category", "seed_query", "layout_fix"):
                kind = "product"
            matched_comps.append(
                SuggestCompletion(
                    text=txt,
                    kind=kind,  # type: ignore[arg-type]
                    score=round(float(c.get("score") or 0.0), 4),
                )
            )
        for fidx in c.get("family_indices") or ():
            if len(prod_hits) >= max_products:
                break
            if 0 <= fidx < len(families):
                fam = families[fidx]
                fid = str(fam.get("family_id") or "")
                if fid and fid not in seen_fids:
                    seen_fids.add(fid)
                    prod_hits.append(_to_product_hit(fam))

    if len(prod_hits) < max_products and families:
        for cname_low, vendor_low, fam in _get_fast_pool(index_state, families):
            if len(prod_hits) >= max_products:
                break
            fid = str(fam.get("family_id") or "")
            if fid in seen_fids:
                continue
            if prefix_norm in cname_low or prefix_norm in vendor_low:
                seen_fids.add(fid)
                prod_hits.append(_to_product_hit(fam))

    return TrieLookupResult(completions=matched_comps, products=prod_hits)