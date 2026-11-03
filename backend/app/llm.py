"""Local Ollama client (default model: qwen3:4b at http://127.0.0.1:11434).

Design for local execution on CPU:
- Uses Ollama's /api/chat endpoint with stream=False and think=False.
- Structured JSON output with format: schema or format: 'json'.
- Defensive stripping of <think>...</think> reasoning tags.
- Robust error handling for service offline, missing model (with clear pull command), and timeouts.
- Disk cache keyed by the full request: same question -> same answer, avoiding redundant CPU computation.
- Localhost security verification ensuring requests never leave the local environment.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import STATE_DIR, settings

def _cache_path():
    from .config import STATE_DIR
    return STATE_DIR / "llm_cache.json"

_lock = threading.RLock()
_state = {"open_until": 0.0, "last_error": "", "calls": 0, "cache_hits": 0, "last_model": ""}
_cache: dict[str, Any] | None = None

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_local_endpoint(url: str) -> bool:
    """Verify endpoint is restricted to localhost / loopback."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return hostname in LOCAL_HOSTS
    except Exception:
        return False


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        c = _load_cache()
        if len(c) > 2000:
            for k in list(c)[: len(c) - 2000]:
                c.pop(k, None)
        _cache_path().write_text(json.dumps(c), encoding="utf-8")
    except Exception:
        pass


def status() -> dict:
    return {
        "enabled": settings.llm_enabled,
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        "circuit_open": time.time() < _state["open_until"],
        "last_error": _state["last_error"],
        "calls": _state["calls"],
        "cache_hits": _state["cache_hits"],
        "last_model": _state["last_model"],
        "narration": settings.llm_narration,
    }


def available() -> bool:
    return settings.llm_enabled and time.time() >= _state["open_until"]


def _strip_thinking(text: str) -> str:
    """Defensively remove <think>...</think> reasoning blocks emitted by models."""
    if not text:
        return ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> Any:
    text = _strip_thinking(text)
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
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
    """Return the assistant text from Ollama /api/chat, or None if the LLM is unavailable / failed."""
    if not available():
        return None

    base_url = settings.ollama_base_url
    if not is_local_endpoint(base_url):
        _state["last_error"] = f"Security error: non-local Ollama base URL '{base_url}' is not allowed (must be localhost / 127.0.0.1)."
        return None

    model = settings.ollama_model
    options: dict[str, Any] = {"temperature": 0.0}
    if max_tokens:
        options["num_predict"] = max_tokens

    base_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
        "think": False,
    }

    cache_key = hashlib.sha256(json.dumps([base_body, schema], sort_keys=True).encode()).hexdigest()
    with _lock:
        c = _load_cache()
        if cache_key in c:
            _state["cache_hits"] += 1
            return c[cache_key]

    # Attempts: if schema given, try schema format first, then format: "json", then base
    attempts = []
    if schema:
        attempts.append({**base_body, "format": schema})
        attempts.append({**base_body, "format": "json"})
    attempts.append(base_body)

    url = f"{base_url}/api/chat"
    headers = {"Content-Type": "application/json"}

    for i, payload in enumerate(attempts):
        for retry in range(2):
            try:
                _state["calls"] += 1
                r = httpx.post(url, headers=headers, json=payload, timeout=settings.llm_timeout)
            except httpx.ConnectError:
                _state["last_error"] = f"Ollama service is not running at {base_url}. Please start Ollama."
                _state["open_until"] = time.time() + 15
                return None
            except httpx.TimeoutException:
                _state["last_error"] = f"Ollama request timed out after {settings.llm_timeout}s"
                _state["open_until"] = time.time() + 15
                return None
            except Exception as e:
                _state["last_error"] = f"Ollama network error: {e}"
                _state["open_until"] = time.time() + 15
                return None

            if r.status_code == 404:
                err_text = r.text.lower()
                if "model" in err_text or "not found" in err_text:
                    _state["last_error"] = f"Model '{model}' not found. Run: ollama pull {model}"
                else:
                    _state["last_error"] = f"Ollama endpoint not found (404): {r.text[:160]}"
                _state["open_until"] = time.time() + 30
                return None

            if r.status_code in (400, 422) and i < len(attempts) - 1:
                _state["last_error"] = f"{r.status_code}: {r.text[:160]}"
                break

            if r.status_code >= 500 and retry == 0:
                time.sleep(1.0)
                continue

            if r.status_code != 200:
                _state["last_error"] = f"Ollama HTTP {r.status_code}: {r.text[:200]}"
                return None

            try:
                data = r.json()
            except Exception as e:
                _state["last_error"] = f"Invalid JSON response from Ollama: {e}"
                return None

            msg = data.get("message")
            if not isinstance(msg, dict):
                _state["last_error"] = "Unexpected response structure: 'message' object missing"
                return None

            text = msg.get("content") or ""
            _state["last_model"] = data.get("model", model)

            text = _strip_thinking(text)

            if not text.strip():
                _state["last_error"] = "empty response"
                break

            with _lock:
                _load_cache()[cache_key] = text
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
