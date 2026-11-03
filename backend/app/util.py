"""Small shared helpers: JSON-safe values, number formatting, names."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def clean(v: Any) -> Any:
    """Make any value JSON safe (NaN -> None, numpy -> python, timestamps -> ISO)."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return None if pd.isna(v) else v.isoformat(sep=" ") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    if v is pd.NaT:
        return None
    if isinstance(v, np.ndarray):
        return [clean(x) for x in v.tolist()]
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def slug(s: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return (s or "x")[:maxlen].strip("_") or "x"


def _style() -> str:
    try:
        from .config import app_settings
        return app_settings().get("number_style", "indian")
    except Exception:
        return "indian"


def group(n: float, decimals: int = 0) -> str:
    """Number with thousands separators in the configured style (Indian: 12,34,567.89)."""
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "-"
    neg = n < 0
    s = f"{abs(n):,.{decimals}f}"
    if _style() == "indian":
        whole, _, frac = f"{abs(n):.{decimals}f}".partition(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            whole = ",".join(parts + [tail])
        s = whole + (("." + frac) if frac else "")
    return ("-" if neg else "") + s


def decimals_for(values, unit_kind: str = "") -> int:
    if unit_kind in ("money",):
        return 2
    if unit_kind == "percent":
        return 2
    try:
        arr = np.asarray([v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))], dtype=float)
    except Exception:
        return 2
    if arr.size == 0:
        return 0
    if np.all(np.abs(arr - np.round(arr)) < 1e-9) and np.nanmax(np.abs(arr)) >= 1:
        return 0
    mx = float(np.nanmax(np.abs(arr)))
    if mx >= 10000:
        return 0
    if mx >= 10:
        return 2
    return 3 if mx < 1 else 2


def fmt_value(v: float | None, unit: str = "", unit_kind: str = "", decimals: int | None = None) -> str:
    """'Rs 4,965.13 lakh', '2.07%', '1,23,456 SCM', '42'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    d = decimals if decimals is not None else decimals_for([v], unit_kind)
    num = group(float(v), d)
    u = (unit or "").strip()
    if unit_kind == "percent" or u in ("%", "percent", "pct"):
        return f"{num}%"
    if unit_kind == "money" or re.match(r"^(rs|inr|₹)\b", u, re.I):
        rest = re.sub(r"^(rs\.?|inr|₹)\s*", "", u, flags=re.I)
        return f"Rs {num}" + (f" {rest}" if rest else "")
    return f"{num} {u}".strip()


def pct(v: float | None, d: int = 1, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "-"
    return f"{v:+.{d}f}%" if sign else f"{v:.{d}f}%"


def nice_label(col: str) -> str:
    """'revenue_rs_lakh' -> 'Revenue (Rs lakh)'; 'kg_sold' -> 'Kg sold'."""
    s = str(col)
    if re.search(r"[A-Z ]", s) and not re.fullmatch(r"[A-Z0-9_]+", s):
        return s.strip()
    toks = [t for t in re.split(r"[_\s]+", s.strip()) if t]
    unit_toks, body = [], []
    UNIT = {"rs": "Rs", "inr": "Rs", "lakh": "lakh", "lac": "lakh", "crore": "crore", "cr": "crore", "scm": "SCM", "mmscm": "MMSCM", "kg": "kg",
            "km": "km", "pct": "%", "percent": "%", "bar": "bar", "scmh": "SCM/h", "kwh": "kWh", "mt": "MT", "hrs": "hours"}
    i = 0
    while i < len(toks):
        tl = toks[i].lower()
        rest = [x.lower() for x in toks[i:]]
        if i > 0 and tl in UNIT and all(x in UNIT or x == "per" for x in rest):
            u = " ".join(UNIT.get(x, "/") if x != "per" else "/" for x in rest).replace(" / ", "/")
            unit_toks.append(u)
            break
        body.append(toks[i])
        i += 1
    words = " ".join(body).replace("  ", " ")
    words = words[:1].upper() + words[1:] if words else s
    words = re.sub(r"\b(id|cng|png|lpg|ufg|scada|drs|gps|kpi|gst|mis|fy|sap|erp)\b", lambda m: m.group(1).upper(), words, flags=re.I)
    if unit_toks:
        u = " ".join(unit_toks).replace("Rs lakh", "Rs lakh")
        return f"{words} ({u})"
    return words


def is_num(x) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))
