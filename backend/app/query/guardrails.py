"""Guardrails: read-only enforcement and out-of-scope detection (run before anything else)."""
from __future__ import annotations

import re

DESTRUCTIVE = re.compile(
    r"\b(delete|drop|truncate|erase|wipe|purge|destroy|remove|update|insert|alter|overwrite|modify|edit|change|set|write off|waive|mita|hata)\b"
    r"[^.?!]{0,60}\b(invoices?|records?|rows?|tables?|data|database|db|customers?|payments?|bills?|complaints?|tickets?|readings?|entries|accounts?|"
    r"files?|sales|dataset|values?|numbers?|figures?|transactions?)\b", re.I)
SQL_WRITE = re.compile(r"^\s*(delete\s+from|drop\s+(table|view|database|schema|index)|truncate\s+(table\s+)?\w+|update\s+\w+\s+set|insert\s+into|alter\s+table|"
                       r"create\s+(table|view|index|database|schema|user)|grant\s+\w+|revoke\s+\w+)\b", re.I)
SAFE_EDIT = re.compile(r"\b(slide|deck|chart|report|section|presentation|title|template|alert|subscription|threshold|label|synonym|name of the column)\b", re.I)
OUT_OF_SCOPE = re.compile(
    r"\b(weather|rain(fall)? tomorrow|temperature outside|forecast for tomorrow|cricket|football|ipl|movie|film|song|recipe|joke|horoscope|stock price|share price|"
    r"sensex|nifty|bitcoin|crypto|election|prime minister|chief minister|latest news|traffic jam|flight|train ticket|hotel booking|restaurant|"
    r"write (a|an) (poem|story|essay))\b", re.I)


def check(text: str, store=None) -> dict | None:
    t = text.strip()
    if SQL_WRITE.match(t) or (DESTRUCTIVE.search(t) and not SAFE_EDIT.search(t)):
        return {"type": "refusal", "intent": "refuse_destructive", "title": "Read-only - request declined",
                "answer": ["I can't do that. SAHAYAK-AI has **read-only** access to your data: it analyses, but it never deletes or changes records.",
                           "Changes must be made in the source system by an authorised user, with an audit trail. To replace a wrong upload, use **Data -> Roll back** or upload a corrected file.",
                           "I can show those records instead - e.g. 'list them' or 'how many are there'."],
                "followups": []}
    if OUT_OF_SCOPE.search(t):
        hit = False
        if store is not None:
            from .understand import rules_plan
            try:
                hit = rules_plan(t, store).get("confidence", 0) >= 0.7
            except Exception:
                hit = False
        if not hit:
            return {"type": "out_of_scope", "intent": "out_of_scope", "title": "Outside the loaded data",
                    "answer": ["That is outside what I can answer. I only answer from the data and documents you have loaded, so I won't guess about outside topics."],
                    "followups": []}
    return None
