"""The LLM path with a fake model: a valid plan is used, invalid plans and invented numbers are rejected."""
import json

import pytest

from app import llm


@pytest.fixture()
def fake_llm(monkeypatch):
    calls = {"plan": None, "text": None}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda msgs, schema, purpose="", max_tokens=500: calls["plan"])
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw: calls["text"])
    from app.config import settings
    monkeypatch.setenv("LLM_NARRATION", "true")
    return calls


def test_llm_plan_used_when_rules_unsure(store, ask, fake_llm):
    t = next(t for t in store.usable() if t.measures() and t.dims())
    fake_llm["plan"] = {"table": t.key, "measures": [{"column": t.measures()[0], "agg": "sum"}], "group_by": [t.dims()[0]], "time_grain": "none", "filters": [],
                        "period_start": "", "period_end": "", "compare": "none", "sort": "desc", "limit": 0, "chart": "bar", "share": False, "mode": "aggregate", "clarify": ""}
    r = ask("zxq frobnicate the wibble numbers please")
    assert r["route"]["chosen_by"] == "llm" and r["type"] == "answer"
    assert all(c["match"] for c in r["checks"])


def test_llm_plan_with_unknown_column_rejected(store, ask, fake_llm):
    t = next(t for t in store.usable() if t.measures())
    fake_llm["plan"] = {"table": t.key, "measures": [{"column": "made_up_column", "agg": "sum"}], "group_by": [], "time_grain": "none", "filters": [],
                        "period_start": "", "period_end": "", "compare": "none", "sort": "none", "limit": 0, "chart": "auto", "share": False, "mode": "aggregate", "clarify": ""}
    r = ask("zxq frobnicate the wibble numbers please")
    assert r["route"].get("llm", "").startswith("unknown column")
    assert r["type"] == "clarify"


def test_invented_numbers_in_summary_dropped(store, ask, fake_llm):
    t = next(t for t in store.usable() if t.measures())
    fake_llm["plan"] = None
    fake_llm["text"] = "The total is 123456789.42 which is great."
    m = t.cols[t.measures()[0]]["label"].split(" (")[0].lower()
    r = ask(f"total {m}")
    assert r["type"] == "answer"
    assert "summary" not in r or "123456789" not in r["summary"]
    assert "dropped" in r.get("narration", "")
