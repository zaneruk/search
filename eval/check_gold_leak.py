# проверка правила R6 (нет строк GOLD10 в seed_glossary)
"""Проверка правила R6 (CONTRACTS.md A.1, A.3, B.11):
в config/seed_glossary.yaml (и коде nlp/) не должно быть строк, дословно совпадающих с GOLD10.
"""
from __future__ import annotations

import csv
import sys
import unicodedata
from pathlib import Path
from typing import Any, List, Tuple

import yaml


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return " ".join(s.split())


def _walk_strings(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    return out


def check_gold_leak(
    gold_csv: Path = Path("eval/datasets/gold_map.csv"),
    glossary_yaml: Path = Path("config/seed_glossary.yaml"),
) -> List[str]:
    if not gold_csv.is_file():
        raise FileNotFoundError(f"GOLD10 map not found: {gold_csv}")
    if not glossary_yaml.is_file():
        raise FileNotFoundError(f"seed_glossary.yaml not found: {glossary_yaml}")

    with gold_csv.open("r", encoding="utf-8") as fh:
        gold_rows = list(csv.DictReader(fh))

    gold_norms = {_norm(r.get("query", "")): r.get("query", "") for r in gold_rows if _norm(r.get("query", ""))}
    data = yaml.safe_load(glossary_yaml.read_text(encoding="utf-8")) or {}

    leaks: List[str] = []
    for path, val in _walk_strings(data):
        v_norm = _norm(val)
        if v_norm in gold_norms:
            leaks.append(f"seed_glossary.yaml:{path} = {val!r} matches GOLD10 {gold_norms[v_norm]!r}")
    return leaks


def main() -> int:
    leaks = check_gold_leak()
    if leaks:
        print("GOLD LEAK DETECTED (R6 violation):")
        for item in leaks:
            print("  -", item)
        return 1
    print("OK: No GOLD10 verbatim leaks in config/seed_glossary.yaml (R6 passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())