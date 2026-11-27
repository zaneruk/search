#!/usr/bin/env python3
"""
Demo-скрипт извлечения совместимости и допродаж со страниц официальных сайтов вендоров (п. 2.3).
Использует httpx + trafilatura + Yandex AI Studio (роль strong = aliceai-llm) через gateway.py.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import httpx

from services.api.llm.gateway import call_llm_structured, get_llm_credentials

try:
    import trafilatura
except ImportError:
    trafilatura = None

DEFAULT_URLS = [
    "https://astralinux.ru/products/astra-linux-special-edition/",
    "https://www.cryptopro.ru/products/csp",
]


def extract_clean_text(html_text: str) -> str:
    if trafilatura is not None:
        extracted = trafilatura.extract(html_text, include_comments=False, include_tables=True)
        if extracted:
            return " ".join(extracted.split())[:6000]
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    plain = re.sub(r"<[^>]+>", " ", no_scripts)
    return " ".join(plain.split())[:6000]


def rule_based_extract(url: str, text: str) -> Dict[str, Any]:
    snippet = text[:200]
    integrations = []
    for os_name in ("Astra Linux", "Windows", "Linux", "РЕД ОС", "macOS", "1С"):
        if os_name.lower() in text.lower():
            integrations.append(os_name)
    return {
        "source_url": url,
        "mode": "rule_fallback",
        "snippet": snippet,
        "integrations": integrations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", nargs="+", default=DEFAULT_URLS)
    parser.add_argument("--out", default="data/interim/demo_vendor_extract.json")
    args = parser.parse_args()

    _, _, api_key, _ = get_llm_credentials()
    prompt_file = Path("prompts/vendor_extract/v1.md")
    system_prompt = (
        prompt_file.read_text(encoding="utf-8")
        if prompt_file.exists()
        else "Extract vendor, product_line, editions, recommended_with, integrations, aliases as JSON."
    )

    results: List[Dict[str, Any]] = []
    headers = {"User-Agent": "SoftlineSmartSearchHackathonBot/1.0 (+https://store.softline.ru)"}

    with httpx.Client(follow_redirects=True, timeout=15.0) as client:
        for url in args.urls:
            print(f"Скачиваем {url} ...")
            try:
                r = client.get(url, headers=headers)
                r.raise_for_status()
                clean_text = extract_clean_text(r.text)
            except Exception as e:
                print(f"  Не удалось скачать {url}: {e}")
                results.append({"source_url": url, "status": "fetch_error", "error": str(e)})
                continue

            if api_key and clean_text:
                parsed, meta = call_llm_structured(
                    role="strong",
                    purpose="vendor_extract",
                    system_prompt=system_prompt,
                    user_payload=f"<user_input>\n{clean_text[:5000]}\n</user_input>",
                    max_tokens_override=500,
                )
                if parsed is not None:
                    parsed["source_url"] = url
                    parsed["snippet"] = clean_text[:200]
                    parsed["mode"] = f"llm_extracted ({meta.get('model')})"
                    results.append(parsed)
                    continue

            results.append(rule_based_extract(url, clean_text))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено в {out_path} ({len(results)} записей).")


if __name__ == "__main__":
    main()