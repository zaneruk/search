"""
Точка входа фонового сервиса Worker (docker-compose сервис `worker`).
Режимы запуска:
  python -m services.worker.scheduler --seed-demo   # мгновенно наполнить БД демо-трафиком и тикетами агента
  python -m services.worker.scheduler --once        # один цикл: диагностика + rollup
  python -m services.worker.scheduler               # бесконечный цикл с интервалом WORKER_INTERVAL_SEC (по умолчанию 300с)
"""

from __future__ import annotations

import argparse
import json
import os
import time

from services.worker.jobs.agent_diagnose import run_agent_diagnose
from services.worker.jobs.kpi_rollup import run_kpi_rollup
from services.worker.jobs.simulator import run_simulator


def run_cycle(simulate_sessions: int = 0, via_api: bool = False) -> dict:
    sim_res = None
    if simulate_sessions > 0:
        sim_res = run_simulator(n_sessions=simulate_sessions, via_api=via_api)
    diag_res = run_agent_diagnose(use_llm=True, max_clusters=15)
    kpi_res = run_kpi_rollup()
    return {
        "simulator": sim_res,
        "agent_diagnose": {
            "clusters_found": diag_res["clusters_found"],
            "created_tickets": diag_res["created_tickets"],
            "updated_tickets": diag_res["updated_tickets"],
        },
        "open_tickets": kpi_res["open_tickets"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker scheduler для аналитики и агента")
    parser.add_argument("--seed-demo", action="store_true", help="Сгенерировать 80 демо-сессий + запустить агента + выйти")
    parser.add_argument("--once", action="store_true", help="Выполнить 1 цикл диагностики и выйти")
    parser.add_argument("--sessions", type=int, default=80, help="Число сессий для --seed-demo")
    parser.add_argument("--via-api", action="store_true", help="Ходить в живой /v1/search при симуляции")
    args = parser.parse_args()

    if args.seed_demo:
        res = run_cycle(simulate_sessions=args.sessions, via_api=args.via_api)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.once:
        res = run_cycle(simulate_sessions=0)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    interval = int(os.getenv("WORKER_INTERVAL_SEC", "300"))
    sim_per_tick = int(os.getenv("WORKER_SIM_SESSIONS", "15"))
    print(f"Worker scheduler запущен (interval={interval}s, sim_per_tick={sim_per_tick})")

    # При первом старте гарантируем, что БД не пустая
    try:
        run_cycle(simulate_sessions=max(20, sim_per_tick), via_api=args.via_api)
    except Exception as e:
        print(f"Ошибка начального цикла воркера: {e}")

    while True:
        time.sleep(interval)
        try:
            res = run_cycle(simulate_sessions=sim_per_tick, via_api=args.via_api)
            print("Worker tick:", json.dumps(res, ensure_ascii=False))
        except Exception as e:
            print(f"Ошибка цикла воркера: {e}")


if __name__ == "__main__":
    main()