"""
Единый LLM-шлюз для Контуров 1, 2, 3 и 5 (CONTRACTS.md B.10, A.12).
Поддерживает Yandex AI Studio (OpenAI-compatible /v1/chat/completions),
автозагрузку .env, резолв URI gpt://<folder_id>/.../latest,
строгий JSON + 1 retry + fallback на LLM_MODEL_FALLBACK,
проверку дневного бюджета (онлайн purpose='qu') и обязательный учёт в cost_ledger (SQLite).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

import httpx
from pydantic import BaseModel

from services.api.app.routers.admin_deps import get_db, utc_now_iso

T = TypeVar("T", bound=BaseModel)

_ENV_LOADED = False


def load_env_file(env_path: Path = Path(".env")) -> None:
    """Лёгкая автозагрузка .env (работает даже без установленного python-dotenv)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def resolve_model_uri(model_or_uri: str, folder_id: Optional[str] = None) -> str:
    """
    Если значение начинается с 'gpt://', возвращает как есть;
    если короткое имя — собирает f'gpt://{YANDEX_FOLDER_ID}/{model}/latest'.
    """
    m = (model_or_uri or "").strip()
    if not m:
        return m
    if m.startswith("gpt://"):
        return m
    fid = (folder_id or os.getenv("YANDEX_FOLDER_ID", "")).strip()
    provider = os.getenv("LLM_PROVIDER", "yandex").strip().lower()
    if provider == "yandex" and fid:
        return f"gpt://{fid}/{m}/latest"
    return m


def get_llm_credentials() -> Tuple[str, str, str, str]:
    """Возвращает (provider, base_url, api_key, folder_id)."""
    load_env_file()
    provider = os.getenv("LLM_PROVIDER", "yandex").strip().lower()
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ("https://ai.api.cloud.yandex.net/v1" if provider == "yandex" else "https://openrouter.ai/api/v1")
    ).strip().rstrip("/")
    api_key = (
        os.getenv("YANDEX_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
    return provider, base_url, api_key, folder_id


def _extract_json_dict(raw_text: str) -> Dict[str, Any]:
    """Надёжно достаёт JSON даже если модель обернула его в ```json ... ``` или пояснение."""
    txt = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt, flags=re.IGNORECASE)
    if fence:
        txt = fence.group(1).strip()
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = txt.find("{")
    end = txt.rfind("}")
    if start != -1 and end > start:
        return json.loads(txt[start : end + 1])
    raise ValueError(f"Ответ LLM не содержит валидный JSON-объект: {txt[:200]}")


def _ensure_snapshot_for_model(conn: sqlite3.Connection, provider: str, model_uri: str) -> Tuple[str, float, float, float, float]:
    """Гарантирует наличие price_snapshot для расчёта стоимости в cost_ledger."""
    snap_file = Path("config/price_snapshots.json")
    if snap_file.exists():
        try:
            items = json.loads(snap_file.read_text(encoding="utf-8"))
            if isinstance(items, dict):
                items = items.get("snapshots", [])
            for item in items:
                if item.get("model") == model_uri or (item.get("model") and item["model"] in model_uri):
                    sid = str(item["snapshot_id"])
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO price_snapshots (
                            snapshot_id, created_at, provider, model,
                            usd_per_mtok_in, usd_per_mtok_cached, usd_per_mtok_out,
                            usd_to_rub, price_source_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid,
                            utc_now_iso(),
                            provider,
                            str(item["model"]),
                            float(item.get("usd_per_mtok_in", 2.0)),
                            float(item.get("usd_per_mtok_cached", 1.0)),
                            float(item.get("usd_per_mtok_out", 6.0)),
                            float(item.get("usd_to_rub", 100.0)),
                            str(item.get("price_source_url", "https://yandex.cloud/ru/docs/foundation-models/pricing")),
                        ),
                    )
                    return (
                        sid,
                        float(item.get("usd_per_mtok_in", 2.0)),
                        float(item.get("usd_per_mtok_cached", 1.0)),
                        float(item.get("usd_per_mtok_out", 6.0)),
                        float(item.get("usd_to_rub", 100.0)),
                    )
        except Exception:
            pass

    low = model_uri.lower()
    if "aliceai-llm-flash" in low:
        sid, p_in, p_c, p_out = "snap_ya_alice_flash", 1.0, 0.5, 3.0
    elif "aliceai-llm" in low:
        sid, p_in, p_c, p_out = "snap_ya_alice_pro", 5.0, 2.5, 10.2
    elif "deepseek" in low:
        sid, p_in, p_c, p_out = "snap_ya_deepseek_flash", 2.0, 0.5, 6.0
    elif "qwen" in low:
        sid, p_in, p_c, p_out = "snap_ya_qwen3_35b", 1.5, 0.75, 4.5
    else:
        sid, p_in, p_c, p_out = "snap_default_mvp", 2.0, 1.0, 6.0

    fx = 100.0
    conn.execute(
        """
        INSERT OR IGNORE INTO price_snapshots (
            snapshot_id, created_at, provider, model,
            usd_per_mtok_in, usd_per_mtok_cached, usd_per_mtok_out,
            usd_to_rub, price_source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            utc_now_iso(),
            provider,
            model_uri,
            p_in,
            p_c,
            p_out,
            fx,
            "https://yandex.cloud/ru/docs/foundation-models/pricing",
        ),
    )
    return sid, p_in, p_c, p_out, fx


def check_daily_budget(conn: sqlite3.Connection) -> Tuple[bool, int, int]:
    """Дневной лимит защищает онлайн-вызовы (purpose='qu') от перерасхода, не блокируя офлайн-работу."""
    load_env_file()
    budget = int(os.getenv("LLM_DAILY_BUDGET_MICRO_RUB", "500000000"))
    today_prefix = conn.execute("SELECT strftime('%Y-%m-%d', 'now')").fetchone()[0]
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_micro_rub), 0) AS s FROM cost_ledger WHERE ts LIKE ? AND purpose = 'qu'",
        (f"{today_prefix}%",),
    ).fetchone()
    spent = int(row["s"] or 0)
    return (spent < budget), spent, budget


def record_cost_ledger(
    conn: sqlite3.Connection,
    purpose: str,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    latency_ms: float,
    status: str,
    trace_id: Optional[str] = None,
    retry_n: int = 0,
) -> int:
    sid, p_in, p_c, p_out, fx = _ensure_snapshot_for_model(conn, provider, model)
    uncached_in = max(0, tokens_in - tokens_cached)
    cost_usd = (
        (uncached_in / 1_000_000.0) * p_in
        + (tokens_cached / 1_000_000.0) * p_c
        + (tokens_out / 1_000_000.0) * p_out
    )
    cost_micro_rub = int(round(cost_usd * fx * 1_000_000.0))

    conn.execute(
        """
        INSERT INTO cost_ledger (
            ts, purpose, provider, model, tokens_in, tokens_cached, tokens_out,
            latency_ms, price_snapshot_id, cost_micro_rub, trace_id, status, retry_n
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            purpose,
            provider,
            model,
            int(tokens_in),
            int(tokens_cached),
            int(tokens_out),
            round(float(latency_ms), 2),
            sid,
            cost_micro_rub,
            trace_id,
            status,
            int(retry_n),
        ),
    )
    return cost_micro_rub


def get_role_model_config(role: str) -> Tuple[str, Optional[str], int, float]:
    """
    Возвращает (model_uri, reasoning_effort, max_completion_tokens, timeout_sec)
    строго по правилам ролей из .env пользователя.
    """
    load_env_file()
    r = role.lower().strip()
    online_timeout = float(os.getenv("LLM_TIMEOUT_MS", "1500")) / 1000.0
    offline_timeout = float(os.getenv("LLM_OFFLINE_TIMEOUT_MS", "30000")) / 1000.0

    if r == "fast":
        m = os.getenv("LLM_MODEL_FAST", "aliceai-llm-flash")
        return resolve_model_uri(m), None, 300, online_timeout
    if r == "strong":
        m = os.getenv("LLM_MODEL_STRONG", "aliceai-llm")
        return resolve_model_uri(m), None, 900, offline_timeout
    if r == "agent":
        m = os.getenv("LLM_MODEL_AGENT", "deepseek-v4.1-flash")
        return resolve_model_uri(m), "low", 2000, offline_timeout
    if r == "synth":
        m = os.getenv("LLM_MODEL_SYNTH", "qwen3.6-35b-a3b")
        return resolve_model_uri(m), "none", 900, offline_timeout
    m = os.getenv("LLM_MODEL_FALLBACK", os.getenv("LLM_MODEL_SYNTH", "qwen3.6-35b-a3b"))
    return resolve_model_uri(m), "none", 900, offline_timeout


def call_llm_structured(
    role: str,
    purpose: str,
    system_prompt: str,
    user_payload: str,
    schema_cls: Optional[Type[T]] = None,
    temperature: float = 0.0,
    max_tokens_override: Optional[int] = None,
    timeout_override: Optional[float] = None,
    trace_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Синхронный вызов LLM со строгим JSON, 1 retry, fallback-моделью и обязательной записью в cost_ledger.
    Возвращает (parsed_dict_or_None, meta_dict).
    """
    provider, base_url, api_key, folder_id = get_llm_credentials()
    if not api_key:
        return None, {"status": "no_api_key", "cost_micro_rub": 0}

    model_uri, effort, default_max_tok, default_timeout = get_role_model_config(role)
    fallback_uri, fb_effort, _, _ = get_role_model_config("fallback")
    max_tok = max_tokens_override or default_max_tok
    timeout_s = timeout_override or default_timeout

    close_after = False
    if conn is None:
        cm = get_db()
        conn = cm.__enter__()
        close_after = True

    try:
        # Ограничение дневного бюджета проверяем для онлайн-поиска ('qu')
        if purpose == "qu":
            budget_ok, spent, limit = check_daily_budget(conn)
            if not budget_ok:
                return None, {"status": "budget_exceeded", "spent": spent, "limit": limit, "cost_micro_rub": 0}

        headers = {"Content-Type": "application/json"}
        if provider == "yandex" and api_key.startswith("AQVN"):
            headers["Authorization"] = f"Api-Key {api_key}"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        if folder_id:
            headers["OpenAI-Project"] = folder_id

        url = f"{base_url}/chat/completions"

        attempts: List[Tuple[str, Optional[str], bool]] = [
            (model_uri, effort, True),
            (model_uri, effort, False),
        ]
        if fallback_uri and fallback_uri != model_uri:
            attempts.append((fallback_uri, fb_effort, False))

        last_err = ""
        cost_micro = 0
        with httpx.Client(timeout=timeout_s) as client:
            for retry_idx, (m_uri, r_eff, use_resp_fmt) in enumerate(attempts):
                body: Dict[str, Any] = {
                    "model": m_uri,
                    "temperature": temperature,
                    "max_completion_tokens": max_tok,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_payload},
                    ],
                }
                if r_eff is not None:
                    body["reasoning_effort"] = r_eff
                if use_resp_fmt:
                    body["response_format"] = {"type": "json_object"}

                t0 = time.perf_counter()
                t_in, t_cached, t_out = 0, 0, 0
                status_str = "error"
                try:
                    resp = client.post(url, headers=headers, json=body)
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    if resp.status_code >= 400:
                        last_err = f"HTTP {resp.status_code}: {resp.text[:250]}"
                        status_str = f"http_{resp.status_code}"
                        continue

                    data = resp.json()
                    usage = data.get("usage", {}) or {}
                    t_in = int(usage.get("prompt_tokens", 0))
                    t_out = int(usage.get("completion_tokens", 0))
                    details = usage.get("prompt_tokens_details") or {}
                    t_cached = int(details.get("cached_tokens", 0))

                    content = data["choices"][0]["message"]["content"]
                    parsed = _extract_json_dict(content)
                    if schema_cls is not None:
                        validated = schema_cls.model_validate(parsed)
                        parsed = validated.model_dump(mode="json")

                    status_str = "ok"
                    return parsed, {
                        "status": "ok",
                        "model": m_uri,
                        "tokens_in": t_in,
                        "tokens_cached": t_cached,
                        "tokens_out": t_out,
                        "latency_ms": round(latency_ms, 2),
                    }
                except Exception as e:
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    last_err = f"{type(e).__name__}: {str(e)[:200]}"
                    status_str = f"error:{type(e).__name__}"
                finally:
                    cost_micro = record_cost_ledger(
                        conn=conn,
                        purpose=purpose,
                        provider=provider,
                        model=m_uri,
                        tokens_in=t_in,
                        tokens_cached=t_cached,
                        tokens_out=t_out,
                        latency_ms=latency_ms,
                        status=status_str,
                        trace_id=trace_id,
                        retry_n=retry_idx,
                    )
                    conn.commit()

        return None, {"status": "failed", "error": last_err, "cost_micro_rub": cost_micro}
    finally:
        if close_after:
            cm.__exit__(None, None, None)