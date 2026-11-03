"""AF-11 number verification: every number in LLM-written text must exist in the computed result.

If any number cannot be traced, the LLM text is rejected and the deterministic narration is used instead."""
from __future__ import annotations

import re
from typing import Any, Iterable

NUM_RE = re.compile(r"(?<![A-Za-z0-9_-])[-+]?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(text or ""):
        s = m.group(0).replace(",", "")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def _walk(o: Any) -> Iterable[Any]:
    if isinstance(o, dict):
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk(v)
    else:
        yield o


def allowed_numbers(*objs: Any) -> set[float]:
    """All numbers that appear anywhere in the computed result (values and text), with rounding variants."""
    vals: set[float] = set()
    for o in objs:
        for v in _walk(o):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                nums = [float(v)]
            elif isinstance(v, str):
                nums = numbers_in(v)
            else:
                continue
            for x in nums:
                vals.add(round(x, 4))
                for d in (0, 1, 2):
                    vals.add(round(x, d))
                vals.add(round(abs(x), 2))
                vals.add(round(abs(x), 1))
                vals.add(round(abs(x), 0))
                if abs(x) >= 100:  # lakh <-> crore
                    vals.add(round(x / 100, 2))
                    vals.add(round(x / 100, 1))
    return vals


def verify_text(text: str, allowed: set[float], extra_ok: Iterable[float] = ()) -> tuple[bool, list[float]]:
    """extra_ok: numbers the user typed in the question (e.g. '3 more months', '2%')."""
    ok_extra = set(extra_ok)
    text = re.sub(r"(?m)^\s*(?:\d+[.)]|[-*\u2022])\s+", "", text or "")  # list markers are not figures
    text = re.sub(r"\bFY\s?\d{2,4}(?:-\d{2})?\b|\bQ[1-4]\b|\bH[12]\b", "", text)  # period labels
    text = re.sub(r"\b[A-Z]{1,6}-[A-Z0-9]{0,6}-?\d+\b", "", text)  # record codes like ABC-XY-01
    text = re.sub(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}-[A-Za-z]{3}(?:-\d{2,4})?\b|\b[A-Za-z]{3}-\d{2}\b", "", text)  # dates
    text = re.sub(r"\b\d{1,2}:\d{2}\b", "", text)  # times
    bad = []
    for n in numbers_in(text):
        if 1900 <= n <= 2100 and float(n).is_integer():  # years
            continue
        r = [round(n, 4), round(n, 2), round(n, 1), round(n, 0), round(abs(n), 2), round(abs(n), 1)]
        if any(x in allowed for x in r) or n in ok_extra:
            continue
        bad.append(n)
    return (len(bad) == 0), bad
