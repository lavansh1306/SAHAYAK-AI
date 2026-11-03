"""Stage 3 - Clean & standardise. Generic rules driven by the column profile; every change is logged."""
from __future__ import annotations

import re
from collections import Counter

import numpy as np
import pandas as pd

from .profile import (ROW_UNITS, TOTAL_RE, is_na, parse_datetimes, parse_months, parse_numbers, period_label)


class Log:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, action: str, detail: str, level: str = "info", column: str | None = None, count: int | None = None):
        self.items.append({"action": action, "detail": detail, "level": level, "column": column, "count": count})


def _examples(vals, k=3) -> str:
    u = []
    for v in vals:
        s = str(v)[:30]
        if s not in u:
            u.append(s)
        if len(u) >= k:
            break
    return ", ".join(f"'{x}'" for x in u)


# ------------------------------------------------------------------------------------- reshaping
def melt_wide(df: pd.DataFrame, wide: dict, log: Log) -> pd.DataFrame:
    per, tot, ids = wide["period_columns"], wide["total_columns"], wide["id_columns"]
    if tot:
        log.add("reshape", f"Total column(s) {', '.join(tot)} dropped - totals are recomputed from the detail, never double counted", "info")
    long = df[ids + per].melt(id_vars=ids, value_vars=per, var_name="period", value_name=wide.get("value_name") or "value")
    labels = {h: period_label(h) for h in per}
    kinds = {k for _, k in labels.values()}
    long["period"] = long["period"].map(lambda h: labels[h][0])
    log.add("reshape", f"Wide layout turned into rows: {len(per)} period columns ({_examples(per, 4)}...) -> one row per {ids[0]} and period"
            + (" (periods read as months)" if kinds == {"month"} else ""), "info")
    return long


def apply_row_units(df: pd.DataFrame, unit_col: str, measure_cols: list[str], log: Log) -> tuple[pd.DataFrame, dict]:
    """A 'Unit' column says the unit of each row: convert every measure in that row to one base unit."""
    kinds, facs, unknown = [], [], 0
    for v in df[unit_col].tolist():
        k = f = None
        if not is_na(v):
            for rx, kind, factor in ROW_UNITS:
                if rx.match(str(v)):
                    k, f = kind, factor
                    break
        if k is None and not is_na(v):
            unknown += 1
        kinds.append(k)
        facs.append(f)
    kind = Counter(k for k in kinds if k).most_common(1)[0][0] if any(kinds) else None
    if not kind:
        return df, {}
    base = {"volume": (1.0, "SCM"), "money": (1e5, "Rs lakh"), "mass": (1.0, "kg")}[kind]
    fac = np.array([(f / base[0]) if (k == kind and f) else np.nan for k, f in zip(kinds, facs)])
    counts = Counter(str(v).strip() for v in df[unit_col].dropna())
    out = df.copy()
    for c in measure_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce") * fac
    log.add("units", f"Rows are in mixed units ({', '.join(f'{k}: {n}' for k, n in counts.most_common())}) - all converted to {base[1]} using the '{unit_col}' column",
            "warn", unit_col, int(sum(counts.values())))
    if unknown:
        log.add("units", f"{unknown} row(s) have an unknown unit - their values were left blank", "warn", unit_col, unknown)
    out = out.rename(columns={unit_col: "unit_reported"})
    return out, {"unit": base[1], "unit_kind": kind}


# ------------------------------------------------------------------------------------- categories
def _canon_case(variants: Counter, existing: set[str]) -> str:
    for v in variants:
        if v in existing:
            return v
    best = variants.most_common()
    top = best[0][1]
    tied = [v for v, n in best if n == top]
    nice = [v for v in tied if not v.isupper() and not v.islower()] or tied
    return sorted(nice)[0]


def normalise_categories(s: pd.Series, existing: list[str] | None, threshold: int, log: Log, col: str) -> pd.Series:
    from rapidfuzz import fuzz
    ex = set(existing or [])
    v = s.map(lambda x: None if is_na(x) else re.sub(r"\s+", " ", str(x)).strip())
    v = v.map(lambda x: x[:-2] if isinstance(x, str) and re.fullmatch(r"-?\d+\.0", x) else x)
    groups: dict[str, Counter] = {}
    for x in v.dropna():
        groups.setdefault(x.lower(), Counter())[x] += 1
    for x in ex:
        groups.setdefault(x.lower(), Counter())
    mapping, changed = {}, []
    for low, cnt in groups.items():
        if not cnt:
            continue
        canon = _canon_case(cnt, ex) if not any(e.lower() == low for e in ex) else next(e for e in ex if e.lower() == low)
        for var in cnt:
            if var != canon:
                mapping[var] = canon
                changed.append((var, canon))
    v = v.map(lambda x: mapping.get(x, x) if x is not None else None)
    if changed:
        log.add("standardise", f"'{col}': spelling/case variants unified ({'; '.join(f'{a!r} -> {b!r}' for a, b in changed[:4])}{' ...' if len(changed) > 4 else ''})",
                "info", col, len(changed))
    # fuzzy: rare spellings close to a frequent / known value
    counts = Counter(v.dropna())
    if 1 < len(counts) <= 500:
        known = sorted(set(counts) | ex, key=lambda k: -counts.get(k, 10 ** 9 if k in ex else 0))
        fz = {}
        for val, n in counts.items():
            for k in known:
                if k == val or k in fz:
                    continue
                kn = counts.get(k, 0) or (10 ** 9 if k in ex else 0)
                if kn < 5 * n:
                    continue
                d1, d2 = re.findall(r"\d+", val), re.findall(r"\d+", k)
                if d1 != d2:
                    continue
                if len(val) >= 4 and fuzz.ratio(val.lower(), k.lower()) >= threshold:
                    fz[val] = k
                    break
        if fz:
            v = v.map(lambda x: fz.get(x, x) if x is not None else None)
            log.add("standardise", f"'{col}': likely typos matched to the common spelling ({'; '.join(f'{a!r} -> {b!r}' for a, b in list(fz.items())[:4])})",
                    "warn", col, len(fz))
    return v


# ------------------------------------------------------------------------------------- main
def fy_text(v) -> str | None:
    if is_na(v):
        return None
    m = re.match(r"^\s*(fy|f\.y\.)?\s*'?(\d{2,4})\s*[-/–]\s*'?(\d{2,4})\s*$", str(v), re.I)
    if m:
        a = int(m.group(2))
        a = a + 2000 if a < 100 else a
        return f"FY{a}-{str(a + 1)[2:]}"
    m = re.match(r"^\s*fy\s*'?(\d{2,4})\s*$", str(v), re.I)
    if m:
        b = int(m.group(1))
        b = b + 2000 if b < 100 else b
        return f"FY{b - 1}-{str(b)[2:]}"
    return str(v).strip()


def standardise(raw: pd.DataFrame, cols: list[dict], opts: dict, existing: dict | None, settings: dict) -> tuple[pd.DataFrame, Log, dict]:
    """raw: columns already renamed to clean names. cols: final column decisions (profile + user overrides)."""
    log = Log()
    n0 = len(raw)
    stats = {"rows_in": n0, "invalid": {}, "dropped": {}}
    df = raw.copy()
    spec = {c["name"]: c for c in cols if c["name"] in df.columns}
    skip = [n for n, c in spec.items() if c.get("role") == "ignore" or c.get("type") == "empty"]
    if skip:
        df = df.drop(columns=skip)
        log.add("columns", f"Empty or ignored column(s) dropped: {', '.join(skip)}", "info")
    # ---- total / sub-total rows (look at the raw text of every non-number column)
    txt_cols = [n for n, c in spec.items() if n in df.columns and c["type"] not in ("number",)]
    if txt_cols:
        tot = pd.Series(False, index=df.index)
        for c in txt_cols:
            tot |= df[c].map(lambda x: isinstance(x, str) and bool(TOTAL_RE.match(x)))
        if tot.any():
            log.add("rows", f"{int(tot.sum())} total / sub-total row(s) removed (they would double count)", "warn", count=int(tot.sum()))
            df = df[~tot]
            stats["dropped"]["total_rows"] = int(tot.sum())
    # ---- parse every column by type
    ex_cols = (existing or {}).get("columns", {})
    for name, c in spec.items():
        if name not in df.columns:
            continue
        t = c["type"]
        s = df[name]
        bad = pd.Series(False, index=df.index)
        if t == "number":
            v, bad, _ = parse_numbers(s)
            df[name] = v
        elif t in ("date", "datetime"):
            order = (opts.get("date_order") or {}).get(name) or c.get("date_order")
            v, bad, info = parse_datetimes(s, order=order if order in ("dayfirst", "monthfirst") else None, excel_serial=bool(c.get("excel_serial")))
            df[name] = v
            if info.get("order") in ("dayfirst", "monthfirst") and info.get("n"):
                log.add("dates", f"'{name}': dates read as {'day/month/year' if info['order'] == 'dayfirst' else 'month/day/year'}"
                        + (" (the file never shows a day above 12, so this was assumed - confirm if unsure)" if info.get("ambiguous") else " (clear from the values)"),
                        "warn" if info.get("ambiguous") else "info", name)
        elif t == "month":
            v, bad = parse_months(s)
            df[name] = v
        elif t == "year":
            v, bad, _ = parse_numbers(s)
            df[name] = v.round().astype("Int64")
        elif t == "fy_label":
            df[name] = s.map(fy_text)
        elif t == "boolean":
            df[name] = s.map(lambda x: None if is_na(x) else ("Yes" if str(x).strip().lower() in ("yes", "y", "true", "t", "1") else "No"))
        else:
            df[name] = s.map(lambda x: None if is_na(x) else re.sub(r"\s+", " ", str(x)).strip()).map(
                lambda x: x[:-2] if isinstance(x, str) and re.fullmatch(r"-?\d+\.0", x) else x)
        nb = int(bad.sum())
        if nb:
            stats["invalid"][name] = nb
            log.add("invalid", f"'{name}': {nb} value(s) could not be read ({_examples(s[bad].tolist())}) - left blank, not guessed", "warn", name, nb)
    # ---- unit alignment with the table this file is joining
    for name, c in spec.items():
        if name not in df.columns or c["type"] != "number" or name not in ex_cols:
            continue
        old = ex_cols[name]
        if old.get("unit_kind") and old.get("unit_kind") == c.get("unit_kind") and old.get("scale") and c.get("scale") and old["scale"] != c["scale"]:
            f = c["scale"] / old["scale"]
            df[name] = df[name] * f
            log.add("units", f"'{name}': this file is in {c.get('unit')} but the table is in {old.get('unit')} - converted (x{f:g})", "warn", name)
            c["unit"], c["scale"] = old.get("unit"), old.get("scale")
    # ---- categories: case, spaces, typos, existing spellings
    thr = int(settings.get("fuzzy_merge_similarity", 92))
    for name, c in spec.items():
        if name in df.columns and c["type"] in ("category",) and c.get("role") == "dimension":
            known = (ex_cols.get(name) or {}).get("values") or []
            df[name] = normalise_categories(df[name], known, thr, log, name)
    # ---- rows without any content
    content = [n for n in df.columns if n in spec and spec[n]["role"] in ("measure", "dimension", "id", "time")]
    if content:
        empty = df[content].isna().all(axis=1)
        if empty.any():
            df = df[~empty]
            stats["dropped"]["blank_rows"] = int(empty.sum())
            log.add("rows", f"{int(empty.sum())} blank row(s) removed", "info", count=int(empty.sum()))
    # ---- rows without a time value cannot be placed in time
    tcol = opts.get("time")
    if tcol and tcol in df.columns:
        nt = df[tcol].isna()
        if nt.any():
            stats["dropped"]["no_time"] = int(nt.sum())
            stats["no_time_share"] = float(nt.mean())
            log.add("rows", f"{int(nt.sum())} row(s) removed because '{tcol}' is empty or unreadable", "warn", tcol, int(nt.sum()))
            df = df[~nt]
    # ---- exact duplicates
    d = df.duplicated()
    if d.any():
        df = df[~d]
        stats["dropped"]["duplicates"] = int(d.sum())
        log.add("rows", f"{int(d.sum())} exact duplicate row(s) removed", "warn", count=int(d.sum()))
    df = df.reset_index(drop=True)
    stats["rows_out"] = len(df)
    return df, log, stats


# ------------------------------------------------------------------------------------- keys / time
TIME_PREF = re.compile(r"^(date|month|period|timestamp|time|day|datetime|billing_month|bill_month|reading_time|txn_date|transaction_date|"
                       r"start_time|trip_date|opened_on|created_at|created_on|invoice_date|bill_date|payment_date)$", re.I)


def primary_time(cols: list[dict], df: pd.DataFrame | None = None) -> str | None:
    tc = [c for c in cols if c.get("role") == "time"]
    if not tc:
        return None

    def score(c):
        s = 0
        if TIME_PREF.match(c["name"]):
            s += 10
        if re.search(r"(due|closed|end|resolved|paid|updated|expiry|valid_to)", c["name"]):
            s -= 5
        if c["type"] in ("month",):
            s += 2
        s += (c.get("filled", 0) / max(c.get("n", 1), 1)) * 3
        return s
    return max(tc, key=score)["name"]


def detect_keys(df: pd.DataFrame, cols: list[dict], tcol: str | None) -> list[str]:
    if df.empty:
        return []
    n = len(df)
    ids = [c["name"] for c in cols if c["type"] == "id" and c["name"] in df.columns]
    for c in ids:
        if df[c].notna().all() and df[c].nunique() == n:
            return [c]
    dims = [c["name"] for c in cols if c.get("role") == "dimension" and c["name"] in df.columns and c["type"] in ("category", "id", "boolean")]
    dims = sorted(dims, key=lambda c: df[c].nunique())
    base = [tcol] if tcol else []
    from itertools import combinations
    for k in range(0, min(4, len(dims)) + 1):
        for combo in combinations(dims, k):
            cand = base + list(combo)
            if not cand:
                continue
            if df.duplicated(cand).sum() == 0 and df[cand].notna().all().all():
                return cand
    return []
