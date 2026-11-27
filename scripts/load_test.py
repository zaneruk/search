"""Нагрузочный прогон >= 500 смешанных запросов (CONTRACTS.md A.12, A.13):
замеряет p50/p95 `/v1/suggest` и `/v1/search` (холодный и прогретый кэш QU) и наполняет cost_ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import List

import numpy as np

from services.api.app.schemas import SearchOptions, SearchRequest, Segment
from services.api.core.index_state import get_index_state
from services.api.core.overlay import get_overlay_store
from services.api.llm.gateway import get_gateway
from services.api.pipeline import execute_search
from services.api.suggest import build_suggest
from services.api.app.main import _load_dotenv_defaults
_load_dotenv_defaults()

def _load_mixed_queries(n: int) -> List[str]:
    queries: List[str] = []
    for p in (Path("eval/datasets/syn_a/queries.jsonl"), Path("eval/datasets/syn_b/queries.jsonl")):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    queries.append(json.loads(line)["query"])
    if not queries:
        queries = ["сервер", "astra linux", "крипто про csp 5.0", "мдаемон", "кибер бэкап", "ghbdtn"]
    out: List[str] = []
    while len(out) < n:
        out.extend(queries)
    return out[:n]


async def run_load_test(n_queries: int = 500, *, llm_enabled: bool = True) -> None:
    idx = get_index_state()
    ov = get_overlay_store()
    ov.load()
    gw = get_gateway()
    await gw.startup()

    queries = _load_mixed_queries(n_queries)
    sug_lat: List[float] = []
    search_l1_lat: List[float] = []
    search_l0_lat: List[float] = []

    try:
        # 1. Suggest (>= 500 префиксов)
        for q in queries:
            t0 = time.perf_counter()
            build_suggest(q[:4], segment=Segment.smb, index_state=idx, overlay=ov.snapshot())
            sug_lat.append((time.perf_counter() - t0) * 1000.0)

        # 2. Search L1 (без LLM)
        for q in queries[:200]:
            t0 = time.perf_counter()
            await execute_search(
                SearchRequest(query=q, session_id="ss_load_l1", options=SearchOptions(llm_enabled=False, include_trace=False)),
                persist_events=False,
                index_state=idx,
                overlay_store=ov,
                gateway=gw,
            )
            search_l1_lat.append((time.perf_counter() - t0) * 1000.0)

        # 3. Search L0 (с LLM + кэшем QU)
        if llm_enabled:
            for q in queries:
                t0 = time.perf_counter()
                await execute_search(
                    SearchRequest(query=q, session_id="ss_load_l0", options=SearchOptions(llm_enabled=True, include_trace=False)),
                    persist_events=False,
                    index_state=idx,
                    overlay_store=ov,
                    gateway=gw,
                )
                search_l0_lat.append((time.perf_counter() - t0) * 1000.0)
    finally:
        await gw.aclose()

    print(f"Suggest (n={len(sug_lat)}): p50={np.percentile(sug_lat, 50):.1f}ms p95={np.percentile(sug_lat, 95):.1f}ms (gate <=60ms)")
    print(f"Search L1 (n={len(search_l1_lat)}): p50={np.percentile(search_l1_lat, 50):.1f}ms p95={np.percentile(search_l1_lat, 95):.1f}ms (gate <=400ms)")
    if search_l0_lat:
        print(f"Search L0 (n={len(search_l0_lat)}): p50={np.percentile(search_l0_lat, 50):.1f}ms p95={np.percentile(search_l0_lat, 95):.1f}ms (gate <=1700ms)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_load_test(args.n, llm_enabled=not args.no_llm))


if __name__ == "__main__":
    main()