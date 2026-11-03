"""Tests for Ollama local LLM integration:
- Request construction (/api/chat, stream=False, think=False, options)
- Response parsing and stripping of <think>...</think> reasoning tags
- Structured JSON output handling
- Error handling: connection offline, model missing (suggesting ollama pull), timeouts
- Non-local base URL blocking (security)
- Ensuring no OpenRouter headers or external fallbacks exist
"""
import json
import pytest
import httpx

from app import llm
from app.config import settings


@pytest.fixture(autouse=True)
def reset_llm_state(monkeypatch):
    monkeypatch.setattr(llm, "_cache", {})
    monkeypatch.setattr(llm, "_state", {"open_until": 0.0, "last_error": "", "calls": 0, "cache_hits": 0, "last_model": ""})
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")


def test_ollama_request_construction(monkeypatch):
    captured = {}

    def mock_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": "Ready"},
            "done": True
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    res = llm.chat([{"role": "user", "content": "ping"}], max_tokens=50)
    assert res == "Ready"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["json"]["model"] == "qwen3:4b"
    assert captured["json"]["stream"] is False
    assert captured["json"]["think"] is False
    assert captured["json"]["options"]["temperature"] == 0.0
    assert captured["json"]["options"]["num_predict"] == 50
    assert captured["json"]["messages"] == [{"role": "user", "content": "ping"}]
    # Verify no OpenRouter authorization headers
    assert "Authorization" not in captured["headers"]


def test_ollama_response_parsing(monkeypatch):
    def mock_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": "The total sales are 500."},
            "done": True
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    out = llm.chat([{"role": "user", "content": "summarise"}])
    assert out == "The total sales are 500."
    assert llm.status()["calls"] == 1
    assert llm.status()["last_model"] == "qwen3:4b"


def test_ollama_thinking_tags_stripped(monkeypatch):
    def mock_post(url, headers=None, json=None, timeout=None):
        content = (
            "<think>\n"
            "The user wants a summary of sales.\n"
            "I should check the numbers carefully.\n"
            "</think>\n"
            "Here is the final summary."
        )
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": content},
            "done": True
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    out = llm.chat([{"role": "user", "content": "summary"}])
    assert out == "Here is the final summary."
    assert "<think>" not in out
    assert "The user wants a summary" not in out


def test_ollama_json_schema_handling(monkeypatch):
    schema = {
        "type": "object",
        "properties": {"table": {"type": "string"}, "measures": {"type": "array"}},
        "required": ["table", "measures"]
    }

    def mock_post(url, headers=None, json=None, timeout=None):
        assert json.get("format") == schema
        content = '{"table": "sales", "measures": ["amount"]}'
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": content},
            "done": True
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    obj = llm.chat_json([{"role": "user", "content": "build plan"}], schema=schema)
    assert obj == {"table": "sales", "measures": ["amount"]}


def test_ollama_json_with_embedded_markdown_and_thinking(monkeypatch):
    schema = {"type": "object", "properties": {"score": {"type": "number"}}}

    def mock_post(url, headers=None, json=None, timeout=None):
        content = "<think>Analyzing score...</think>\n```json\n{\"score\": 95.5}\n```"
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "message": {"role": "assistant", "content": content},
            "done": True
        })

    monkeypatch.setattr(httpx, "post", mock_post)

    obj = llm.chat_json([{"role": "user", "content": "score"}], schema=schema)
    assert obj == {"score": 95.5}


def test_ollama_service_offline_error(monkeypatch):
    def mock_post(url, headers=None, json=None, timeout=None):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "post", mock_post)

    res = llm.chat([{"role": "user", "content": "test"}])
    assert res is None
    st = llm.status()
    assert "Ollama service is not running" in st["last_error"]
    assert "Please start Ollama" in st["last_error"]
    assert st["circuit_open"] is True


def test_ollama_missing_model_error(monkeypatch):
    def mock_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(404, text='{"error": "model \'qwen3:4b\' not found, try pulling it first"}')

    monkeypatch.setattr(httpx, "post", mock_post)

    res = llm.chat([{"role": "user", "content": "test"}])
    assert res is None
    st = llm.status()
    assert "Model 'qwen3:4b' not found" in st["last_error"]
    assert "ollama pull qwen3:4b" in st["last_error"]


def test_ollama_timeout_error(monkeypatch):
    def mock_post(url, headers=None, json=None, timeout=None):
        raise httpx.TimeoutException("Read timed out")

    monkeypatch.setattr(httpx, "post", mock_post)

    res = llm.chat([{"role": "user", "content": "test"}])
    assert res is None
    st = llm.status()
    assert "timed out" in st["last_error"]


def test_ollama_security_rejects_external_endpoint(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://external-api.openrouter.ai/v1")

    res = llm.chat([{"role": "user", "content": "test"}])
    assert res is None
    st = llm.status()
    assert "Security error" in st["last_error"]
    assert "must be localhost / 127.0.0.1" in st["last_error"]

