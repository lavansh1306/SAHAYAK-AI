"""OpenRouter client (default model: openrouter/free).

Design for accuracy and robustness with free models:
- temperature 0, JSON-schema structured output when supported (falls back to json_object, then plain text)
- short timeouts, 1 retry, and a circuit breaker after rate limits so the app degrades to rule-based mode
- disk cache keyed by the full request: same question -> same answer, and fewer calls against free-tier quotas
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

import httpx

from .config import STATE_DIR, settings

CACHE_PATH = STATE_DIR / "llm_cache.json"
_lock = threading.RLock()
_state = {"open_until": 0.0, "last_error": "", "calls": 0, "cache_hits": 0, "last_model": ""}
_cache: dict[str, Any] | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        c = _load_cache()
        if len(c) > 2000:
            for k in list(c)[: len(c) - 2000]:
                c.pop(k, None)
        CACHE_PATH.write_text(json.dumps(c), encoding="utf-8")
    except Exception:
        pass


def status() -> dict:
    return {"enabled": settings.llm_enabled, "model": settings.openrouter_model, "base_url": settings.openrouter_base_url,
            "key_present": bool(settings.openrouter_api_key), "circuit_open": time.time() < _state["open_until"],
            "last_error": _state["last_error"], "calls": _state["calls"], "cache_hits": _state["cache_hits"], "last_model": _state["last_model"],
            "narration": settings.llm_narration}


def available() -> bool:
    return settings.llm_enabled and time.time() >= _state["open_until"]


def _extract_json(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def chat(messages: list[dict], *, schema: dict | None = None, max_tokens: int = 700, purpose: str = "") -> str | None:
    """Return the assistant text, or None if the LLM is unavailable / failed."""
    if not available():
        return None
    body = {"model": settings.openrouter_model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    key = hashlib.sha256(json.dumps([body, schema], sort_keys=True).encode()).hexdigest()
    with _lock:
        c = _load_cache()
        if key in c:
            _state["cache_hits"] += 1
            return c[key]
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json",
               "HTTP-Referer": "http://localhost:5173", "X-Title": "SAHAYAK-AI"}
    attempts = []
    if schema:
        attempts.append({**body, "response_format": {"type": "json_schema", "json_schema": {"name": purpose or "output", "strict": True, "schema": schema}}})
        attempts.append({**body, "response_format": {"type": "json_object"}})
    attempts.append(body)
    url = f"{settings.openrouter_base_url}/chat/completions"
    for i, payload in enumerate(attempts):
        for retry in range(2):
            try:
                _state["calls"] += 1
                r = httpx.post(url, headers=headers, json=payload, timeout=settings.llm_timeout)
            except Exception as e:  # network problems -> open circuit briefly
                _state["last_error"] = f"network: {e}"
                _state["open_until"] = time.time() + 30
                return None
            if r.status_code == 429:
                _state["last_error"] = "rate limited (429)"
                if retry == 0:
                    time.sleep(2.0)
                    continue
                _state["open_until"] = time.time() + 60
                return None
            if r.status_code in (400, 404, 422) and i < len(attempts) - 1:
                _state["last_error"] = f"{r.status_code}: {r.text[:160]}"
                break  # try next, simpler request format
            if r.status_code >= 500 and retry == 0:
                time.sleep(1.0)
                continue
            if r.status_code != 200:
                _state["last_error"] = f"{r.status_code}: {r.text[:200]}"
                if r.status_code in (401, 402, 403):
                    _state["open_until"] = time.time() + 300
                return None
            try:
                data = r.json()
                text = data["choices"][0]["message"].get("content") or ""
                _state["last_model"] = data.get("model", "")
            except Exception as e:
                _state["last_error"] = f"bad response: {e}"
                return None
            if not text.strip():
                _state["last_error"] = "empty response"
                break
            with _lock:
                _load_cache()[key] = text
                _save_cache()
            _state["last_error"] = ""
            return text
    return None


def chat_json(messages: list[dict], schema: dict, purpose: str = "", max_tokens: int = 500) -> dict | None:
    t = chat(messages, schema=schema, purpose=purpose, max_tokens=max_tokens)
    if t is None:
        return None
    obj = _extract_json(t)
    return obj if isinstance(obj, dict) else None
