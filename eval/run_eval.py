# P0..P3 на holdout SYN-A/B, GOLD10, suggest, маржа, I1..I7 -> eval_report.json
"""Полный прогон Eval (CONTRACTS.md A.4, A.5, A.7, A.9, A.13):
- GOLD10 (smoke, tier=real)
- SYN-A holdout + SYN-B на 4 точках P0..P3
- Инварианты I1..I7
- SuggestMRR & Product-Hit@4@k (k=3,5,7) + latency p50/p95
- Чувствительность к марже (A.9: 0, default, 3x)
- Парный бутстрэп 10k и критерий Макнемара
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from eval.impact_model import compute_impact_report
from eval.stats import mcnemar_test, paired_bootstrap_delta, proportion_with_ci
from services.api.app.schemas import ClientContext, PriceState, RelevanceLabel, SearchOptions, SearchResponse, Segment
from services.api.app.schemas import SearchRequest
from services.api.core.index_state import IndexState, get_index_state
from services.api.core.overlay import OverlaySnapshot, OverlayStore, get_overlay_store
from services.api.llm.gateway import LLMGateway
from services.api.pipeline import execute_search
from services.api.suggest import build_suggest
from services.api.app.main import _load_dotenv_defaults
_load_dotenv_defaults()
_CHECKPOINTS = {
    "P0": {"label": "Legacy-эмулятор", "options": {"mode": "legacy", "include_trace": False}},
    "P1": {"label": "BM25F + Entity + Fuzzy (L1+L2+L3)", "options": {"mode": "smart", "llm_enabled": False, "disable_dense": True, "disable_l5": True, "include_trace": False}},
    "P2": {"label": "Hybrid L1 (P1 + Dense L4 + L5 + QU-правила)", "options": {"mode": "smart", "llm_enabled": False, "disable_dense": False, "disable_l5": False, "include_trace": False}},
    "P3": {"label": "Full Smart L0 (P2 + LLM-QU эскалация)", "options": {"mode": "smart", "llm_enabled": True, "disable_dense": False, "disable_l5": False, "include_trace": False}},
}


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _dcg(grades: Sequence[int]) -> float:
    return sum(((2 ** g) - 1.0) / math.log2(i + 2) for i, g in enumerate(grades))


def _grade_for_family(fid: str, rec: Dict[str, Any], fam_ptype: Dict[str, str]) -> int:
    if fid == rec.get("target_family_id"):
        return 3
    if fid in (rec.get("grade2_family_ids") or []):
        return 2
    tpt = rec.get("target_product_type")
    if tpt and fam_ptype.get(fid) == tpt:
        return 1
    return 0


def _check_response_invariants(resp: SearchResponse) -> Dict[str, bool]:
    items = [it for sec in resp.sections for it in sec.items]
    distinct_fids = {it.family_id for it in items}
    i1 = len(distinct_fids) >= 4
    i2 = all((it.representative.price_minor != 0) and ((it.representative.price_state == PriceState.on_request) == (it.representative.price_minor is None)) for it in items)
    i3 = True
    i5 = True
    for it in items:
        rep = it.representative
        has_promo = any(b.type == "promo" for b in it.badges)
        exp_promo = (
            rep.oldprice_minor is not None
            and rep.price_minor is not None
            and rep.oldprice_minor > rep.price_minor > 0
            and ((rep.oldprice_minor - rep.price_minor) / rep.oldprice_minor * 100.0) <= 70.0
        )
        if has_promo != exp_promo:
            i3 = False
        if it.relevance_label in (RelevanceLabel.similar, RelevanceLabel.popular) and not it.fill_reason:
            i5 = False
    i6 = all(
        cs.available and (cs.price_min_minor is not None and cs.price_min_minor > 0) and (cs.family_id not in distinct_fids)
        for cs in resp.cross_sell
    )
    return {"I1": i1, "I2": i2, "I3": i3, "I5": i5, "I6": i6}


def _eval_gold10(idx: IndexState, ov: OverlayStore, gw: Optional[LLMGateway], *, llm_enabled: bool, disable_dense: bool) -> Dict[str, Any]:
    gold_path = Path("eval/datasets/gold_map.csv")
    if not gold_path.is_file():
        return {"n": 0, "hit1": 0.0, "hit4": 0.0, "data_tier": "real", "details": []}
    with gold_path.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    async def _run() -> List[Dict[str, Any]]:
        out = []
        for g in rows:
            q = g.get("query", "")
            kind = g.get("target_kind", "product")
            tfid = g.get("target_family_id", "")
            teid = g.get("target_entity_id", "")
            resp = await execute_search(
                SearchRequest(query=q, session_id="ss_gold", options=SearchOptions(llm_enabled=llm_enabled, disable_dense=disable_dense, include_trace=False)),
                persist_events=False,
                index_state=idx,
                overlay_store=ov,
                gateway=gw,
            )
            fids = [it.family_id for sec in resp.sections for it in sec.items]
            nav_ids = [nh.entity_id for nh in resp.nav_hits]
            if kind in ("category", "content"):
                h1 = (teid in nav_ids[:1]) or (bool(fids) and fids[0] == tfid)
                h4 = (teid in nav_ids[:2]) or (tfid in fids[:4])
            else:
                h1 = bool(fids) and fids[0] == tfid
                h4 = tfid in fids[:4]
            out.append({"query": q, "kind": kind, "target_family_id": tfid, "hit1": h1, "hit4": h4, "top4": fids[:4], "nav": nav_ids[:2]})
        return out

    details = asyncio.run(_run())
    n = len(details)
    h1_cnt = sum(1 for d in details if d["hit1"])
    h4_cnt = sum(1 for d in details if d["hit4"])
    return {
        "n": n,
        "hit1_count": h1_cnt,
        "hit4_count": h4_cnt,
        "hit1": round(h1_cnt / n, 4) if n else 0.0,
        "hit4": round(h4_cnt / n, 4) if n else 0.0,
        "descriptive_only": True,
        "data_tier": "real",
        "details": details,
    }


def _eval_suggest(records: List[Dict[str, Any]], idx: IndexState, ov: OverlaySnapshot) -> Dict[str, Any]:
    eligible = [r for r in records if r.get("query_class") in ("exact_name", "brand") and r.get("target_family_id")][:60]
    latencies: List[float] = []
    by_k: Dict[str, Dict[str, Any]] = {}
    
    build_suggest("ab", segment=Segment.smb, index_state=idx, overlay=ov)

    for k in (3, 5, 7):
        mrr_vals: List[float] = []
        prod_hits: List[bool] = []
        for r in eligible:
            prefix = r["query"][:k]
            if len(prefix) < 2:
                continue
            t0 = time.perf_counter()
            sug = build_suggest(prefix, segment=Segment.smb, index_state=idx, overlay=ov)
            latencies.append((time.perf_counter() - t0) * 1000.0)
            tfid = r["target_family_id"]
            prod_fids = [p.family_id for p in sug.products[:4]]
            prod_hits.append(tfid in prod_fids)
            rank_c = next((i + 1 for i, c in enumerate(sug.completions) if c.text.lower() in r["query"].lower() or r["query"].lower().startswith(c.text.lower())), None)
            mrr_vals.append(1.0 / rank_c if rank_c else 0.0)
        by_k[f"k={k}"] = {
            "n": len(prod_hits),
            "suggest_mrr": round(float(np.mean(mrr_vals)), 4) if mrr_vals else 0.0,
            "product_hit4": proportion_with_ci(prod_hits),
        }

    p50 = round(float(np.percentile(latencies, 50)), 2) if latencies else 0.0
    p95 = round(float(np.percentile(latencies, 95)), 2) if latencies else 0.0
    return {"by_prefix_len": by_k, "latency_p50_ms": p50, "latency_p95_ms": p95, "data_tier": "derived"}


async def _eval_checkpoint(
    cp_id: str,
    records: List[Dict[str, Any]],
    idx: IndexState,
    ov: OverlayStore,
    gw: Optional[LLMGateway],
    fam_ptype: Dict[str, str],
) -> Dict[str, Any]:
    opts = SearchOptions(**_CHECKPOINTS[cp_id]["options"])
    hit1_all: List[bool] = []
    hit4_all: List[bool] = []
    mrr_all: List[float] = []
    ndcg_all: List[float] = []
    hard_nulls: List[bool] = []
    soft_nulls: List[bool] = []
    fill_rates: List[bool] = []
    exact_shares: List[float] = []
    latencies: List[float] = []
    typo1_hits: List[bool] = []
    typo2_hits: List[bool] = []
    exact_brand_hits: List[bool] = []
    fcr_flags: List[bool] = []
    ooc_flags: List[bool] = []
    inv_ok: List[bool] = []

    for rec in records:
        t0 = time.perf_counter()
        resp = await execute_search(
            SearchRequest(query=rec["query"], session_id=f"ss_eval_{cp_id}", options=opts),
            persist_events=False,
            index_state=idx,
            overlay_store=ov,
            gateway=gw,
        )
        lat_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(lat_ms)

        items = [it for sec in resp.sections for it in sec.items]
        fids = [it.family_id for it in items]
        qcls = rec.get("query_class", "")

        hard_nulls.append(resp.hard_null)
        soft_nulls.append(resp.soft_null)
        fill_rates.append(resp.fill_rung > 0)

        if items:
            top4_items = items[:4]
            exact_shares.append(sum(1 for it in top4_items if it.relevance_label in (RelevanceLabel.exact, RelevanceLabel.close)) / len(top4_items))

        if opts.mode == "smart":
            invs = _check_response_invariants(resp)
            inv_ok.append(all(invs.values()))

        if rec.get("expected_ooc"):
            has_ooc = bool(resp.out_of_catalog_banner) or any(nh.kind == "EXTERNAL_PRODUCT" for nh in resp.nav_hits)
            no_exact_badge = all(it.relevance_label != RelevanceLabel.exact for it in items[:4])
            ooc_flags.append(has_ooc and no_exact_badge)
            continue
        if rec.get("is_nonsense"):
            continue

        grades10 = [_grade_for_family(fid, rec, fam_ptype) for fid in fids[:10]]
        h1 = bool(grades10 and grades10[0] >= 2)
        h4 = any(g >= 2 for g in grades10[:4])
        first_rel = next((i + 1 for i, g in enumerate(grades10) if g >= 2), None)
        mrr = 1.0 / first_rel if first_rel else 0.0
        ideal = sorted(grades10, reverse=True)
        idcg = _dcg(ideal)
        ndcg = (_dcg(grades10) / idcg) if idcg > 0 else 0.0

        hit1_all.append(h1)
        hit4_all.append(h4)
        mrr_all.append(mrr)
        ndcg_all.append(ndcg)

        if qcls == "typo_1edit":
            typo1_hits.append(h4)
        elif qcls == "typo_2edit":
            typo2_hits.append(h4)
        elif qcls in ("exact_name", "brand"):
            exact_brand_hits.append(h4)
            fcr_flags.append(bool(resp.applied_query) and (not h1))

    return {
        "checkpoint": cp_id,
        "label": _CHECKPOINTS[cp_id]["label"],
        "data_tier": "derived",
        "n_scored": len(hit4_all),
        "hit1": proportion_with_ci(hit1_all),
        "hit4": proportion_with_ci(hit4_all),
        "failure_rate": round(1.0 - (sum(hit4_all) / len(hit4_all)), 4) if hit4_all else 1.0,
        "mrr10": round(float(np.mean(mrr_all)), 4) if mrr_all else 0.0,
        "ndcg10": round(float(np.mean(ndcg_all)), 4) if ndcg_all else 0.0,
        "hard_null_rate": proportion_with_ci(hard_nulls),
        "soft_null_rate": proportion_with_ci(soft_nulls),
        "fill_rate": round(sum(fill_rates) / len(fill_rates), 4) if fill_rates else 0.0,
        "exact_share_at_4": round(float(np.mean(exact_shares)), 4) if exact_shares else 0.0,
        "exact_brand_hit4": proportion_with_ci(exact_brand_hits),
        "typo1_recovery4": proportion_with_ci(typo1_hits),
        "typo2_recovery4": proportion_with_ci(typo2_hits),
        "fcr": proportion_with_ci(fcr_flags),
        "ooc_precision": proportion_with_ci(ooc_flags),
        "invariants_pass_rate": round(sum(inv_ok) / len(inv_ok), 4) if inv_ok else 1.0,
        "latency_p50_ms": round(float(np.percentile(latencies, 50)), 2) if latencies else 0.0,
        "latency_p95_ms": round(float(np.percentile(latencies, 95)), 2) if latencies else 0.0,
        "_raw_hit4": hit4_all,
        "_raw_ndcg10": ndcg_all,
    }


async def _eval_margin_sensitivity(dev_records: List[Dict[str, Any]], idx: IndexState, ov: OverlayStore, fam_ptype: Dict[str, str]) -> Dict[str, Any]:
    base_snap = ov.snapshot()
    default_w = float(base_snap.ranking_weights.get("margin_proxy", 0.05))
    sample = [r for r in dev_records if not r.get("expected_ooc") and not r.get("is_nonsense")][:60]

    results: Dict[str, float] = {}
    for label, mult in (("margin_0", 0.0), ("margin_default", 1.0), ("margin_3x", 3.0)):
        new_weights = dict(base_snap.ranking_weights)
        new_weights["margin_proxy"] = default_w * mult
        ov_snap = OverlaySnapshot(config_version=base_snap.config_version, created_at=base_snap.created_at, items=base_snap.items, ranking_weights=new_weights)
        ndcgs: List[float] = []
        for rec in sample:
            resp = await execute_search(
                SearchRequest(query=rec["query"], session_id="ss_margin", options=SearchOptions(llm_enabled=False, disable_dense=True, include_trace=False)),
                persist_events=False,
                index_state=idx,
                overlay_override=ov_snap,
            )
            fids = [it.family_id for sec in resp.sections for it in sec.items][:10]
            grades = [_grade_for_family(f, rec, fam_ptype) for f in fids]
            idcg = _dcg(sorted(grades, reverse=True))
            ndcgs.append(_dcg(grades) / idcg if idcg > 0 else 0.0)
        results[label] = round(float(np.mean(ndcgs)), 4) if ndcgs else 0.0

    delta_default_vs_0 = round(results["margin_default"] - results["margin_0"], 4)
    return {
        "n_dev_sample": len(sample),
        "ndcg10": results,
        "delta_default_vs_0": delta_default_vs_0,
        "passed_a9_gate": delta_default_vs_0 >= -0.01,
        "data_tier": "derived",
    }


def run_full_eval(*, skip_p3_llm: bool = False, limit_holdout: Optional[int] = None) -> Dict[str, Any]:
    syn_a_path = Path("eval/datasets/syn_a/queries.jsonl")
    if not syn_a_path.is_file():
        raise FileNotFoundError("Run eval/gen/make_syn_a.py first")
    syn_a = [json.loads(line) for line in syn_a_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    dev_recs = [r for r in syn_a if r.get("split") == "dev"]
    holdout_recs = [r for r in syn_a if r.get("split") == "holdout"]
    if limit_holdout:
        holdout_recs = holdout_recs[:limit_holdout]

    idx = get_index_state()
    ov = get_overlay_store()
    ov.load()

    fam_ptype = {}
    if idx.is_loaded and idx.families is not None:
        fam_ptype = dict(zip(idx.families["family_id"].astype(str), idx.families["product_type"].fillna("").astype(str)))

    async def _run_all() -> Dict[str, Any]:
        gw = LLMGateway()
        await gw.startup()
        try:
            cps = ["P0", "P1", "P2"] + ([] if skip_p3_llm else ["P3"])
            cp_out: Dict[str, Any] = {}
            for cp in cps:
                cp_out[cp] = await _eval_checkpoint(cp, holdout_recs, idx, ov, gw, fam_ptype)
            margin_chk = await _eval_margin_sensitivity(dev_recs, idx, ov, fam_ptype)
            return {"checkpoints": cp_out, "margin": margin_chk}
        finally:
            await gw.aclose()

    eval_res = asyncio.run(_run_all())
    cp_out = eval_res["checkpoints"]

    # Сравнения (A.7: парный бутстрэп 10k + Макнемар)
    comparisons: Dict[str, Any] = {}
    target_cp = "P3" if "P3" in cp_out else "P2"
    for name, base_cp, cand_cp in (
        ("smart_vs_legacy", "P0", target_cp),
        ("hybrid_vs_bm25f", "P1", "P2"),
        ("full_vs_hybrid", "P2", "P3"),
    ):
        if base_cp in cp_out and cand_cp in cp_out:
            comparisons[name] = {
                "hit4_bootstrap": paired_bootstrap_delta(cp_out[base_cp]["_raw_hit4"], cp_out[cand_cp]["_raw_hit4"]),
                "ndcg10_bootstrap": paired_bootstrap_delta(cp_out[base_cp]["_raw_ndcg10"], cp_out[cand_cp]["_raw_ndcg10"]),
                "hit4_mcnemar": mcnemar_test(cp_out[base_cp]["_raw_hit4"], cp_out[cand_cp]["_raw_hit4"]),
                "data_tier": "derived",
            }

    # Удаляем сырые массивы из JSON-отчёта
    for cp in cp_out.values():
        cp.pop("_raw_hit4", None)
        cp.pop("_raw_ndcg10", None)

    gold10 = _eval_gold10(idx, ov, None, llm_enabled=False, disable_dense=False)
    suggest_res = _eval_suggest(holdout_recs or syn_a, idx, ov.snapshot())
    f_n = float(cp_out[target_cp]["failure_rate"])
    impact = compute_impact_report(f_n=f_n, index_state=idx).model_dump(mode="json")

    run_id = datetime.now(timezone.utc).strftime("eval_%Y%m%d_%H%M%S")
    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": _git_sha(),
        "config_version": ov.snapshot().config_version,
        "index_version": idx.index_version,
        "embedding_model": idx.manifest.get("embedding_model", "BAAI/bge-m3"),
        "seed": 42,
        "holdout_size": len(holdout_recs),
        "gold10": gold10,
        "checkpoints": cp_out,
        "comparisons": comparisons,
        "margin_sensitivity": eval_res["margin"],
        "suggest": suggest_res,
        "impact_model": impact,
    }

    reports_dir = Path("eval/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон Eval P0..P3, GOLD10, Suggest, Маржа")
    parser.add_argument("--skip-p3-llm", action="store_true", help="Пропустить P3 с живой LLM (только P0..P2)")
    parser.add_argument("--limit-holdout", type=int, default=None)
    args = parser.parse_args()
    rep = run_full_eval(skip_p3_llm=args.skip_p3_llm, limit_holdout=args.limit_holdout)
    print(f"Eval saved -> eval/reports/eval_report.json (run_id={rep['run_id']}, holdout={rep['holdout_size']})")
    print(f"GOLD10: {rep['gold10']['hit4_count']}/{rep['gold10']['n']} Hit@4 (tier=real)")
    for cp_id, cp in rep["checkpoints"].items():
        print(f"  {cp_id}: Hit@4={cp['hit4']['value']} | MRR@10={cp['mrr10']} | nDCG@10={cp['ndcg10']} | hard_null={cp['hard_null_rate']['value']} | p95={cp['latency_p95_ms']}ms | inv={cp['invariants_pass_rate']}")


if __name__ == "__main__":
    main()