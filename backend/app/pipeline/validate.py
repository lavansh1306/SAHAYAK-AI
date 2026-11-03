"""Stage 4 - Validate: generic data checks + automatic discovery of formulas between columns.

Formulas found (e.g. revenue = volume x rate x 0.00001, loss = input - output, loss% = loss / input x 100) are used
to (1) flag rows that break them and (2) make averages of rates and percentages correct: an average rate is
computed as total numerator / total denominator, never as a plain average of row values."""
from __future__ import annotations

import itertools
import re
from datetime import datetime

import numpy as np
import pandas as pd

from ..util import group

NICE_K = [1.0, 100.0, 0.01, 1e-5, 1e5, 1000.0, 0.001, 1e-7, 1e7, 1e-6, 1e6, 60.0, 1 / 60, 24.0, 1 / 24, 1e-3 * 100, 1e-2 / 100]


def _snap(k: float) -> float | None:
    for c in NICE_K:
        if c and abs(k / c - 1) < 0.003:
            return c
    return None


def _decimals(v: np.ndarray) -> int:
    for d in range(0, 5):
        if np.all(np.abs(v * 10 ** d - np.round(v * 10 ** d)) < 1e-6):
            return d
    return 6


def find_relations(df: pd.DataFrame, measures: list[str], max_cols: int = 12) -> list[dict]:
    """Exact-ish arithmetic relations between numeric columns (held by >= 97% of rows)."""
    ms = [m for m in measures if m in df.columns and df[m].notna().mean() > 0.6][:max_cols]
    if len(ms) < 3:
        return []
    sub = df[ms].dropna()
    if len(sub) > 20000:
        sub = sub.sample(20000, random_state=1)
    if len(sub) < 12:
        return []
    A = {m: sub[m].to_numpy(dtype=float) for m in ms}
    dec = {m: _decimals(A[m][:500]) for m in ms}
    rels = []

    def tol(c):
        return np.maximum(1.0 * 10.0 ** (-dec[c]), 0.004 * np.abs(A[c]))

    def held(c, pred):
        ok = np.abs(A[c] - pred) <= tol(c)
        return ok.mean()

    for c in ms:
        others = [m for m in ms if m != c]
        for a, b in itertools.combinations(others, 2):
            if np.allclose(A[a], A[c]) or np.allclose(A[b], A[c]):
                continue
            for kind, pred, parts in (("sum", A[a] + A[b], [a, b]), ("difference", A[a] - A[b], [a, b]), ("difference", A[b] - A[a], [b, a])):
                if np.abs(A[c]).sum() > 0 and held(c, pred) >= 0.97:
                    op = "+" if kind == "sum" else "-"
                    rels.append({"kind": kind, "target": c, "parts": parts, "formula": f"{c} = {parts[0]} {op} {parts[1]}", "factor": 1.0})
                    break
            nz = (A[a] != 0) & (A[b] != 0) & (A[c] != 0)
            if nz.mean() > 0.6:
                r = A[c][nz] / (A[a][nz] * A[b][nz])
                k = _snap(float(np.median(r)))
                if k and held(c, k * A[a] * A[b]) >= 0.97:
                    rels.append({"kind": "product", "target": c, "parts": [a, b], "factor": k,
                                 "formula": f"{c} = {a} x {b}" + (f" x {k:g}" if k != 1 else "")})
                for num, den in ((a, b), (b, a)):
                    nz2 = (A[den] != 0) & (A[num] != 0)
                    if nz2.mean() < 0.6:
                        continue
                    r2 = A[c][nz2] * A[den][nz2] / A[num][nz2]
                    k2 = _snap(float(np.median(r2)))
                    if k2 and held(c, np.divide(k2 * A[num], A[den], out=np.full_like(A[num], np.nan), where=A[den] != 0)) >= 0.97:
                        rels.append({"kind": "ratio", "target": c, "parts": [num, den], "factor": k2,
                                     "formula": f"{c} = {num} / {den}" + (f" x {k2:g}" if k2 != 1 else "")})
    # de-duplicate (same columns) and keep the simplest
    out, seen = [], set()
    for r in rels:
        key = (r["target"], tuple(sorted(r["parts"])), r["kind"] in ("product", "ratio"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def apply_relations(cols: dict[str, dict], rels: list[dict]) -> list[str]:
    """Rates and percentages that are a ratio of two additive columns are averaged as ratio of sums."""
    notes = []
    for r in rels:
        if r["kind"] == "ratio":
            c, (num, den) = r["target"], r["parts"]
            if cols.get(c, {}).get("agg") != "sum" and cols.get(num, {}).get("agg") == "sum" and cols.get(den, {}).get("agg") == "sum":
                cols[c]["agg"] = "ratio"
                cols[c]["ratio"] = {"num": num, "den": den, "factor": r["factor"]}
                notes.append(f"'{c}' is averaged as total {num} / total {den}" + (f" x {r['factor']:g}" if r["factor"] != 1 else ""))
        elif r["kind"] == "product":
            c, (a, b) = r["target"], r["parts"]
            for rate, other in ((a, b), (b, a)):
                if cols.get(rate, {}).get("agg") == "mean" and cols.get(other, {}).get("agg") == "sum" and cols.get(c, {}).get("agg") == "sum":
                    cols[rate]["agg"] = "ratio"
                    cols[rate]["ratio"] = {"num": c, "den": other, "factor": 1 / r["factor"]}
                    notes.append(f"'{rate}' is averaged as total {c} / total {other}" + (f" x {1 / r['factor']:g}" if r["factor"] != 1 else "") + " (weighted, not a plain average)")
                    break
    return notes


def _fy_of(month: str, start: int) -> int:
    y, m = int(month[:4]), int(month[5:7])
    return y if m >= start else y - 1


def validate(df: pd.DataFrame, cols: dict[str, dict], tcol: str | None, keys: list[str], stats: dict, settings: dict, fy_start_month: int,
             today: datetime | None = None) -> tuple[list[dict], list[dict], float, bool]:
    """-> checks, relations, quality score, quarantine?"""
    checks: list[dict] = []
    q = False
    n = len(df)
    maxbad = float(settings.get("max_invalid_share", 0.2))

    def add(check, status, detail):
        checks.append({"check": check, "status": status, "detail": detail})

    if n == 0:
        add("Usable rows", "fail", "No usable rows after cleaning")
        return checks, [], 0.0, True
    add("Usable rows", "pass", f"{group(n)} rows ({group(stats.get('rows_in', n))} in the file)")
    if stats.get("missing_keys"):
        add("Matches the target table", "fail", f"The file has no {', '.join(stats['missing_keys'])} column - rows cannot be matched to the existing table. "
            "Upload again and either map those columns in the review or load it as a new table. Quarantined.")
        q = True
    keys = [k for k in (keys or []) if k in df.columns]
    # unreadable values
    for c, nb in (stats.get("invalid") or {}).items():
        share = nb / max(stats.get("rows_in", n), 1)
        role = cols.get(c, {}).get("role")
        if role == "time" and c == tcol and share > maxbad:
            add(f"Readable {c}", "fail", f"{group(nb)} of {group(stats.get('rows_in', n))} values unreadable ({share:.0%}) - the time column is not reliable, table quarantined")
            q = True
        elif role == "measure" and share > 0.5:
            add(f"Readable {c}", "warn", f"{group(nb)} values unreadable ({share:.0%}) - most of this column is not numeric")
        else:
            add(f"Readable {c}", "warn", f"{group(nb)} value(s) unreadable ({share:.1%}) - left blank")
    if stats.get("no_time_share", 0) > maxbad:
        add("Time values", "fail", f"{stats['no_time_share']:.0%} of rows have no usable '{tcol}' - quarantined")
        q = True
    # measures: emptiness, negatives, outliers
    measures = [c for c, p in cols.items() if p.get("role") == "measure" and c in df.columns]
    if measures and all(df[m].isna().mean() > (1 - 1e-9) for m in measures):
        add("Numbers present", "fail", "Every numeric column is empty after cleaning - quarantined")
        q = True
    for m in measures:
        v = df[m].dropna()
        if len(v) == 0:
            continue
        nulls = df[m].isna().mean()
        if nulls > 0.5:
            add(f"Filled {m}", "warn", f"{nulls:.0%} of rows have no value")
        neg = int((v < 0).sum())
        if neg and neg / len(v) < 0.02:
            add(f"Sign of {m}", "warn", f"{neg} negative value(s) in a column that is otherwise positive")
        if len(v) >= 30:
            med = float(v.median())
            mad = float((v - med).abs().median()) * 1.4826
            if mad > 0:
                z = (v - med).abs() / mad
                k = int((z > 10).sum())
                if k:
                    add(f"Unusual values in {m}", "pass",
                        f"{k} value(s) far from the typical value (median {group(med, 2)}) - kept as they are; see Insights for what they mean")
    # keys
    if keys:
        dk = int(df.duplicated(keys).sum())
        add("Unique rows", "warn" if dk else "pass", f"{dk} rows repeat the same {' + '.join(keys)}" if dk else f"One row per {' + '.join(keys)}")
    # time checks
    if tcol and tcol in df.columns:
        tt = cols[tcol]["type"]
        today = today or datetime.now()
        if tt in ("date", "datetime"):
            future = int((df[tcol] > pd.Timestamp(today) + pd.Timedelta(days=1)).sum())
            if future:
                add("Future dates", "warn", f"{future} row(s) dated after today")
            d = df[tcol].dropna()
            days = d.dt.normalize().drop_duplicates().sort_values()
            if len(days) > 3:
                step = days.diff().median()
                full = pd.date_range(days.min(), days.max(), freq="D")
                if step == pd.Timedelta(days=1) and len(days) / max(len(full), 1) >= 0.8:
                    miss = len(full) - len(days)
                    add("Time coverage", "warn" if miss else "pass", f"{miss} day(s) missing between {days.min():%d-%b-%Y} and {days.max():%d-%b-%Y}" if miss else
                        f"Every day present from {days.min():%d-%b-%Y} to {days.max():%d-%b-%Y}")
        elif tt == "month":
            ms = sorted(df[tcol].dropna().unique())
            if len(ms) > 1:
                y0, m0 = int(ms[0][:4]), int(ms[0][5:])
                y1, m1 = int(ms[-1][:4]), int(ms[-1][5:])
                exp = (y1 - y0) * 12 + (m1 - m0) + 1
                miss = exp - len(ms)
                add("Time coverage", "warn" if miss else "pass", f"{miss} month(s) missing between {ms[0]} and {ms[-1]}" if miss else f"Every month present from {ms[0]} to {ms[-1]}")
                now = f"{today.year:04d}-{today.month:02d}"
                fut = [m for m in ms if m > now]
                if fut:
                    add("Future months", "warn", f"{len(fut)} month(s) after the current month ({fut[0]} ...)")
    # financial-year label columns vs the time column
    for c, p in cols.items():
        if p.get("type") == "fy_label" and tcol and c in df.columns:
            tt = cols[tcol]["type"]
            mon = df[tcol] if tt == "month" else (df[tcol].dt.strftime("%Y-%m") if tt in ("date", "datetime") else None)
            if mon is None:
                continue
            lab = df[c].astype(str).str.extract(r"FY(\d{4})", expand=False)
            ok = lab.notna() & mon.notna()
            if ok.sum() == 0:
                continue
            exp = mon[ok].map(lambda m: _fy_of(m, fy_start_month))
            bad = int((lab[ok].astype(int) != exp).sum())
            if bad:
                i = (lab[ok].astype(int) != exp).idxmax()
                add(f"Financial-year label '{c}'", "warn",
                    f"{group(bad)} of {group(int(ok.sum()))} rows have a label that does not match the financial year starting in month {fy_start_month} "
                    f"(e.g. {mon[i]} labelled {df.at[i, c]}). Questions use the financial year worked out from '{tcol}' instead.")
                p["mismatch"] = True
            else:
                add(f"Financial-year label '{c}'", "pass", "Matches the financial year of every row")
    # formulas between numeric columns
    rels = find_relations(df, measures)
    for r in rels:
        a = df[r["parts"][0]]
        b = df[r["parts"][1]]
        c = df[r["target"]]
        k = r["factor"]
        pred = {"sum": a + b, "difference": a - b, "product": a * b * k, "ratio": k * a / b.replace(0, np.nan)}[r["kind"]]
        dec = _decimals(c.dropna().to_numpy()[:500]) if c.notna().any() else 2
        tolv = np.maximum(1.0 * 10.0 ** (-dec), 0.004 * c.abs())
        bad = int(((c - pred).abs() > tolv).sum())
        pretty = r["formula"].replace(" x 1e-05", " / 100,000").replace(" x 100", " x 100")
        add(f"Formula {r['target']}", "warn" if bad else "pass", f"{pretty}: " + (f"{bad} row(s) do not add up" if bad else "holds for every row"))
        r["violations"] = bad
    # quality score
    pen = 0.0
    for ch in checks:
        if ch["status"] == "warn":
            pen += 3
        elif ch["status"] == "fail":
            pen += 40
    for c, nb in (stats.get("invalid") or {}).items():
        pen += min(10.0, 100.0 * nb / max(stats.get("rows_in", n), 1))
    return checks, rels, round(max(0.0, 100.0 - pen), 1), q
