"""Execute a query plan. Every figure is computed twice - pandas and an independently written DuckDB SQL query -
and the two are compared. Rates and percentages are averaged as ratio of totals; 'latest value' columns
(stock, active connections ...) are never summed across time."""
from __future__ import annotations

import ast
import re
import threading
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

from ..config import fy_start
from ..util import nice_label
from .periods import Period, add_months, describe, fit_period, fy_label, month_long

_DLOCK = threading.Lock()
AGG_LABEL = {"sum": "Total", "mean": "Average", "ratio": "Average", "count": "Number of", "count_distinct": "Number of distinct", "min": "Minimum",
             "max": "Maximum", "median": "Median", "last": "Latest", "avg_per": "Average per"}


class PlanError(Exception):
    pass


# ------------------------------------------------------------------------------------------- helpers
def q(c: str) -> str:
    return '"' + str(c).replace('"', '""') + '"'


def lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _norm(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower()


def joined_frame(store, t, need: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Bring dimension columns from linked tables (many-to-one, up to 2 hops)."""
    df = t.df
    notes = []
    missing = [c for c in need if c and c not in df.columns]
    if not missing:
        return df, notes
    df = df.copy()
    frontier = [(t.key, None)]
    seen = {t.key}
    for _ in range(2):
        nxt = []
        for k, _p in frontier:
            for l in store.links:
                if l["child"] != k or l["parent"] in seen:
                    continue
                p = store.tables.get(l["parent"])
                if not p:
                    continue
                if l["parent_col"] in missing and l["child_col"] in df.columns:
                    df[l["parent_col"]] = df[l["child_col"]]
                    missing = [c for c in missing if c != l["parent_col"]]
                take = [c for c in missing if c in p.df.columns]
                hop = [x["child_col"] for x in store.links if x["child"] == p.key and x["child_col"] in p.df.columns and x["child_col"] not in df.columns]
                cols = list(dict.fromkeys([l["parent_col"]] + take + hop))
                if len(cols) == 1 or l["child_col"] not in df.columns:
                    continue
                right = p.df[cols].drop_duplicates(l["parent_col"])
                if l["parent_col"] in df.columns and l["parent_col"] != l["child_col"]:
                    right = right.rename(columns={l["parent_col"]: f"__{l['parent_col']}"})
                    df = df.merge(right, left_on=l["child_col"], right_on=f"__{l['parent_col']}", how="left").drop(columns=[f"__{l['parent_col']}"])
                else:
                    df = df.merge(right, left_on=l["child_col"], right_on=l["parent_col"], how="left", suffixes=("", "__dup"))
                    if l["parent_col"] != l["child_col"] and l["parent_col"] in df.columns and l["parent_col"] not in t.df.columns:
                        pass
                if take:
                    notes.append(f"{', '.join(take)} taken from {p.label} (linked by {l['child_col']})")
                missing = [c for c in missing if c not in df.columns]
                seen.add(p.key)
                nxt.append((p.key, l))
        frontier = nxt
        if not missing:
            break
    if missing:
        raise PlanError(f"Column(s) {', '.join(missing)} are not in {t.label} or any linked table")
    return df, notes


def time_parts(t, df: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    """-> (timestamp series, 'YYYY-MM' series) for the table's time column."""
    c = t.time
    if not c or c not in df.columns:
        return None, None
    tt = t.time_type
    if tt == "month":
        m = df[c].astype("string")
        return pd.to_datetime(m + "-01", errors="coerce"), m
    if tt == "year":
        y = pd.to_numeric(df[c], errors="coerce")
        ts = pd.to_datetime(y.astype("Int64").astype("string") + "-01-01", errors="coerce")
        return ts, ts.dt.strftime("%Y-%m")
    ts = pd.to_datetime(df[c], errors="coerce")
    return ts, ts.dt.strftime("%Y-%m")


def bucket(ts: pd.Series, month: pd.Series, grain: str) -> pd.Series:
    S = fy_start()
    if grain == "hour":
        return ts.dt.strftime("%Y-%m-%d %H:00")
    if grain == "day":
        return ts.dt.strftime("%Y-%m-%d")
    if grain == "week":
        return (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    if grain == "month":
        return month
    y = month.str[:4].astype(float)
    m = month.str[5:7].astype(float)
    if grain == "year":
        return month.str[:4]
    fy = np.where(m >= S, y, y - 1)
    fyl = pd.Series([fy_label(int(v)) if not np.isnan(v) else None for v in fy], index=month.index)
    if grain == "fy":
        return fyl
    qn = ((m - S) % 12) // 3 + 1
    return fyl + " Q" + pd.Series(qn, index=month.index).astype("Int64").astype("string")


def sql_bucket(t, grain: str) -> str:
    c = q(t.time)
    S = fy_start()
    tt = t.time_type
    if tt == "month":
        if grain == "month":
            return c
        y, m = f"CAST(substr({c},1,4) AS INTEGER)", f"CAST(substr({c},6,2) AS INTEGER)"
        if grain == "year":
            return f"substr({c},1,4)"
    else:
        if tt == "year":
            c = f"make_date(CAST({c} AS INTEGER),1,1)"
        if grain == "hour":
            return f"strftime({c}, '%Y-%m-%d %H:00')"
        if grain == "day":
            return f"strftime({c}, '%Y-%m-%d')"
        if grain == "week":
            return f"strftime(date_trunc('week', {c}), '%Y-%m-%d')"
        if grain == "month":
            return f"strftime({c}, '%Y-%m')"
        if grain == "year":
            return f"strftime({c}, '%Y')"
        y, m = f"year({c})", f"month({c})"
    fy = f"(CASE WHEN {m} >= {S} THEN {y} ELSE {y} - 1 END)"
    lab = f"('FY' || CAST({fy} AS VARCHAR))" if S == 1 else f"('FY' || CAST({fy} AS VARCHAR) || '-' || right(CAST({fy} + 1 AS VARCHAR), 2))"
    if grain == "fy":
        return lab
    return f"({lab} || ' Q' || CAST(((({m} - {S}) % 12 + 12) % 12) // 3 + 1 AS VARCHAR))"


# ------------------------------------------------------------------------------------------- periods
def available_months(month: pd.Series) -> list[str]:
    return sorted(month.dropna().unique().tolist()) if month is not None else []


def resolve_period(plan: dict, t, ts, month, notes: list[str]) -> dict | None:
    """Fit the requested period to the data; fill defaults. -> {start,end,d0,d1,label,kind}"""
    p = plan.get("period")
    av = available_months(month)
    if not av:
        return None
    if p and p.get("kind") == "days":
        d0, d1 = pd.Timestamp(p["d0"]), pd.Timestamp(p["d1"]) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        n = int(((ts >= d0) & (ts <= d1)).sum())
        if n == 0:
            lo, hi = ts.min(), ts.max()
            if p.get("relative"):
                span = (pd.Timestamp(p["d1"]) - pd.Timestamp(p["d0"])).days
                e = hi.normalize()
                s = e - pd.Timedelta(days=span)
                notes.append(f"No data for {p['label']}; the latest data is {hi:%d-%b-%Y}, so the last {span + 1} day(s) of data are used.")
                return {"start": s.strftime("%Y-%m"), "end": e.strftime("%Y-%m"), "d0": s.strftime("%Y-%m-%d"), "d1": e.strftime("%Y-%m-%d"),
                        "label": f"{s:%d-%b-%Y} to {e:%d-%b-%Y}" if span else f"{e:%d-%b-%Y}", "kind": "days"}
            raise PlanError(f"No data for {p['label']}. {t.label} covers {lo:%d-%b-%Y} to {hi:%d-%b-%Y}.")
        return p
    if p:
        per = Period(p["start"], p["end"], p.get("label") or describe(p["start"], p["end"]), p.get("kind", "range"), bool(p.get("relative")))
        fit, n2 = fit_period(per, av)
        notes += n2
        if fit is None:
            raise PlanError(" ".join(n2) or f"No data for {per.label}")
        return {"start": fit.start, "end": fit.end, "label": fit.label, "kind": fit.kind}
    return None


def period_mask(plan_period: dict | None, t, ts, month) -> pd.Series:
    if plan_period is None or ts is None:
        return pd.Series(True, index=ts.index if ts is not None else None)
    if plan_period.get("d0"):
        d0, d1 = pd.Timestamp(plan_period["d0"]), pd.Timestamp(plan_period["d1"]) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        return (ts >= d0) & (ts <= d1)
    return (month >= plan_period["start"]) & (month <= plan_period["end"])


def sql_period(plan_period: dict | None, t) -> str:
    if not plan_period or not t.time:
        return ""
    c = q(t.time)
    if t.time_type == "month":
        if plan_period.get("d0"):
            return f"{c} BETWEEN {lit(plan_period['d0'][:7])} AND {lit(plan_period['d1'][:7])}"
        return f"{c} BETWEEN {lit(plan_period['start'])} AND {lit(plan_period['end'])}"
    if t.time_type == "year":
        return f"CAST({c} AS INTEGER) BETWEEN {int(plan_period['start'][:4])} AND {int(plan_period['end'][:4])}"
    if plan_period.get("d0"):
        d0, d1 = plan_period["d0"], plan_period["d1"]
    else:
        d0 = plan_period["start"] + "-01"
        e = pd.Timestamp(plan_period["end"] + "-01") + pd.offsets.MonthEnd(0)
        d1 = e.strftime("%Y-%m-%d")
    return f"{c} >= TIMESTAMP {lit(d0 + ' 00:00:00')} AND {c} <= TIMESTAMP {lit(d1 + ' 23:59:59')}"


def shift_period(p: dict, how: str) -> dict:
    if p.get("d0"):
        d0, d1 = pd.Timestamp(p["d0"]), pd.Timestamp(p["d1"])
        if how == "previous_year":
            a, b = d0 - pd.DateOffset(years=1), d1 - pd.DateOffset(years=1)
        else:
            span = (d1 - d0).days + 1
            a, b = d0 - pd.Timedelta(days=span), d0 - pd.Timedelta(days=1)
        return {"start": a.strftime("%Y-%m"), "end": b.strftime("%Y-%m"), "d0": a.strftime("%Y-%m-%d"), "d1": b.strftime("%Y-%m-%d"),
                "label": f"{a:%d-%b-%Y}" + (f" to {b:%d-%b-%Y}" if a != b else ""), "kind": "days"}
    n = (int(p["end"][:4]) - int(p["start"][:4])) * 12 + int(p["end"][5:]) - int(p["start"][5:]) + 1
    k = 12 if how == "previous_year" else n
    s, e = add_months(p["start"], -k), add_months(p["end"], -k)
    per = Period(p["start"], p["end"], p.get("label", ""), p.get("kind", "range"))
    lab = per.shift_years(-1).label if how == "previous_year" and p.get("kind") in ("fy", "h1", "h2", "quarter") else describe(s, e)
    return {"start": s, "end": e, "label": lab, "kind": p.get("kind", "range")}


# ------------------------------------------------------------------------------------------- aggregation
def measure_info(t, m: dict) -> dict:
    c = m["col"]
    e = t.cols.get(c, {}) if c not in (None, "*") else {}
    metric = (t.entry.get("metrics") or {}).get(c) if c else None
    agg = m.get("agg") or (e.get("agg") if e.get("role") == "measure" else None) or ("metric" if metric else "count_distinct" if c not in (None, "*") else "count")
    if agg == "ratio" and not e.get("ratio"):
        agg = "mean"
    if metric:
        agg = "metric"
    tl = t.label.strip()
    label = metric["label"] if metric else (e.get("label") or nice_label(c)) if c not in (None, "*") else (tl.lower() if tl.lower().endswith("s") else f"{tl.lower()} records")
    unit = (metric or {}).get("unit", "") if metric else e.get("unit", "")
    kind = (metric or {}).get("unit_kind", "") if metric else e.get("unit_kind", "")
    if agg in ("count", "count_distinct"):
        unit, kind = "", "count"
    head = AGG_LABEL.get(agg, "")
    if agg == "avg_per":
        head = f"Average per {m.get('grain', 'month')}"
    if agg == "metric":
        head = ""
    if agg == "count_distinct":
        head = "Number of"
        label = (e.get("label") or nice_label(c)).replace(" ID", "").replace(" id", "") + ("s" if not str(e.get("label", c)).endswith("s") else "")
    text = f"{head} {label[:1].lower() + label[1:] if head and not label[:2].isupper() else label}".strip()
    return {"col": c, "agg": agg, "label": label, "text": text, "unit": unit, "unit_kind": kind, "ratio": e.get("ratio"), "metric": metric, "grain": m.get("grain")}


def _metric_eval(expr: str, g, df, keys):
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / (b.replace(0, np.nan) if isinstance(b, pd.Series) else (b or np.nan))
        if isinstance(n, ast.UnaryOp):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Call):
            f, c = n.func.id, n.args[0].id
            if f == "count":
                return g.size().astype(float)
            if f == "count_distinct":
                return g[c].nunique().astype(float)
            return getattr(g[c], {"avg": "mean", "last": "last"}.get(f, f))().astype(float)
        raise PlanError("bad metric")
    return ev(tree)


def aggregate(df: pd.DataFrame, keys: list[str], mi: dict, tkey: pd.Series | None, extra_bucket: pd.Series | None = None) -> pd.Series:
    """One measure -> Series indexed by keys (or a 1-row Series indexed by 'All')."""
    d = df.copy()
    k = list(keys) or ["__all__"]
    if not keys:
        d["__all__"] = "All"
    g = d.groupby(k, dropna=True, sort=False)
    a, c = mi["agg"], mi["col"]
    if a == "count":
        r = g.size().astype(float)
    elif a == "count_distinct":
        r = g[c].nunique().astype(float)
    elif a == "ratio":
        num, den, f = mi["ratio"]["num"], mi["ratio"]["den"], mi["ratio"]["factor"]
        sn, sd = g[num].sum(min_count=1), g[den].sum(min_count=1)
        r = sn / sd.replace(0, np.nan) * f
    elif a == "last":
        d["__t"] = tkey.loc[d.index] if tkey is not None else 0
        mx = d.groupby(k, dropna=True)["__t"].transform("max")
        r = d[d["__t"] == mx].groupby(k, dropna=True, sort=False)[c].sum(min_count=1)
    elif a == "avg_per":
        d["__b"] = extra_bucket.loc[d.index]
        inner = d.groupby(k + ["__b"], dropna=True)[c].sum(min_count=1)
        r = inner.groupby(level=list(range(len(k)))).mean()
    elif a == "metric":
        r = _metric_eval(mi["metric"]["expr"], g, d, k)
    elif a in ("sum",):
        r = g[c].sum(min_count=1)
    else:
        r = getattr(g[c], a)()
    return r.astype(float)


def sql_measure(t, mi: dict, keys_sql: list[tuple[str, str]], where: str, extra_bucket_sql: str | None) -> str:
    """Independent SQL for one measure. keys_sql: [(alias, expression)]."""
    sel_k = ", ".join(f"{e} AS {q(a)}" for a, e in keys_sql)
    grp = ", ".join(e for _, e in keys_sql)
    nn = " AND ".join(f"{e} IS NOT NULL" for _, e in keys_sql)
    w = " AND ".join(x for x in [where, nn] if x)
    W = f" WHERE {w}" if w else ""
    a, c = mi["agg"], mi["col"]
    if a == "count":
        ex = "COUNT(*)"
    elif a == "count_distinct":
        ex = f"COUNT(DISTINCT {q(c)})"
    elif a == "ratio":
        r = mi["ratio"]
        ex = f"SUM({q(r['num'])}) / NULLIF(SUM({q(r['den'])}), 0) * {r['factor']!r}"
    elif a == "sum":
        ex = f"SUM({q(c)})"
    elif a == "mean":
        ex = f"AVG({q(c)})"
    elif a in ("min", "max", "median"):
        ex = f"{a.upper()}({q(c)})"
    elif a == "last":
        part = f"PARTITION BY {grp}" if grp else ""
        inner_k = ", ".join(f"{e} AS {q(a2)}" for a2, e in keys_sql)
        tc = q(t.time)
        return (f"SELECT {', '.join(q(a2) for a2, _ in keys_sql) + ', ' if keys_sql else ''}SUM(v) AS v FROM (SELECT {inner_k + ', ' if inner_k else ''}{q(c)} AS v, {tc} AS tt, "
                f"MAX({tc}) OVER ({part}) AS mx FROM t{W}) s WHERE tt = mx" + (f" GROUP BY {', '.join(q(a2) for a2, _ in keys_sql)}" if keys_sql else ""))
    elif a == "avg_per":
        inner_k = ", ".join(f"{e} AS {q(a2)}" for a2, e in keys_sql)
        ks = ", ".join(q(a2) for a2, _ in keys_sql)
        return (f"SELECT {ks + ', ' if ks else ''}AVG(s) AS v FROM (SELECT {inner_k + ', ' if inner_k else ''}{extra_bucket_sql} AS b, SUM({q(c)}) AS s FROM t{W} "
                f"GROUP BY {grp + ', ' if grp else ''}b) x" + (f" GROUP BY {ks}" if ks else ""))
    elif a == "metric":
        def rep(m):
            f, col = m.group(1), m.group(2)
            if f == "count":
                return "COUNT(*)"
            if f == "count_distinct":
                return f"COUNT(DISTINCT {q(col)})"
            return f"{ {'avg': 'AVG', 'mean': 'AVG', 'sum': 'SUM', 'min': 'MIN', 'max': 'MAX', 'median': 'MEDIAN', 'last': 'LAST'}[f]}({q(col)})"
        ex = re.sub(r"\b(sum|avg|mean|count_distinct|count|min|max|median|last)\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", rep, mi["metric"]["expr"])
        ex = re.sub(r"/\s*([A-Z]+\([^)]*\))", r"/ NULLIF(\1, 0)", ex)
    else:
        raise PlanError(f"unsupported aggregation {a}")
    return f"SELECT {sel_k + ', ' if sel_k else ''}{ex} AS v FROM t{W}" + (f" GROUP BY {grp}" if grp else "")


def sql_where(plan: dict, t, period: dict | None, df: pd.DataFrame) -> str:
    parts = []
    sp = sql_period(period, t)
    if sp:
        parts.append(sp)
    for f in plan.get("filters", []):
        c, op, vs = f["col"], f["op"], f.get("values") or []
        if op in ("in", "not_in"):
            inner = ", ".join(lit(str(v).strip().lower()) for v in vs)
            parts.append(f"lower(trim(CAST({q(c)} AS VARCHAR))) {'NOT IN' if op == 'not_in' else 'IN'} ({inner})")
        elif op == "contains":
            parts.append(f"lower(CAST({q(c)} AS VARCHAR)) LIKE {lit('%' + str(vs[0]).lower() + '%')}")
        elif op == "between":
            parts.append(f"{q(c)} BETWEEN {float(vs[0])} AND {float(vs[1])}")
        else:
            parts.append(f"{q(c)} {op} {float(vs[0])}")
    return " AND ".join(parts)


def filter_mask(df: pd.DataFrame, plan: dict) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for f in plan.get("filters", []):
        c, op, vs = f["col"], f["op"], f.get("values") or []
        if c not in df.columns:
            raise PlanError(f"Unknown column {c}")
        if op in ("in", "not_in"):
            hit = _norm(df[c]).isin({str(v).strip().lower() for v in vs}) & df[c].notna()
            m &= ~hit if op == "not_in" else hit
        elif op == "contains":
            m &= _norm(df[c]).str.contains(re.escape(str(vs[0]).lower()), na=False)
        else:
            x = pd.to_numeric(df[c], errors="coerce")
            v = float(vs[0])
            m &= {">": x > v, ">=": x >= v, "<": x < v, "<=": x <= v, "between": (x >= v) & (x <= float(vs[-1]))}[op]
    return m


# ------------------------------------------------------------------------------------------- run
def run(store, plan: dict) -> dict:
    t = store.get(plan["table"])
    if t is None:
        raise PlanError(f"Table {plan['table']} is not loaded")
    notes: list[str] = []
    need = list(plan.get("group", [])) + [f["col"] for f in plan.get("filters", [])]
    df, jn = joined_frame(store, t, need)
    notes += jn
    ts, month = time_parts(t, df)
    mis = [measure_info(t, m) for m in plan["measures"]]
    for mi in mis:
        if mi["col"] not in (None, "*") and mi["agg"] != "metric" and mi["col"] not in df.columns:
            raise PlanError(f"Unknown column {mi['col']}")
    grain = plan.get("grain") if t.time else None
    # default period: 'latest value' measures without a time breakdown use the latest period only
    period = resolve_period(plan, t, ts, month, notes) if ts is not None else None
    if period is None and ts is not None and plan.get("compare"):
        last = month.max()
        S = fy_start()
        y, m = int(last[:4]), int(last[5:])
        fy0 = y if m >= S else y - 1
        start = f"{fy0:04d}-{S:02d}"
        period = {"start": start, "end": last, "label": describe(start, last) + (" (financial year to date)" if start != last else ""), "kind": "range"}
        notes.append(f"No period was given, so the latest financial year to date ({describe(start, last)}) is compared with the same months a year earlier.")
    if period is None and ts is not None and not grain and any(mi["agg"] == "last" for mi in mis):
        last = month.max()
        period = {"start": last, "end": last, "label": month_long(last), "kind": "month"}
        notes.append(f"'{mis[0]['label']}' is a point-in-time figure, so the latest month in the data ({month_long(last)}) is used.")
    base_mask = filter_mask(df, plan)
    pm = period_mask(period, t, ts, month) if ts is not None else pd.Series(True, index=df.index)
    sel = df[base_mask & pm]
    if plan.get("mode") == "rows":
        cols = [c for c in df.columns if c in t.cols and not t.cols[c].get("hidden") and t.cols[c].get("role") != "ignore"]
        out = sel[cols]
        if plan.get("sort") and plan["measures"] and plan["measures"][0]["col"] in out.columns:
            out = out.sort_values(plan["measures"][0]["col"], ascending=plan["sort"] == "asc")
        return {"mode": "rows", "table": t, "rows": out.head(plan.get("limit") or 200), "total": len(out), "period": period, "notes": notes,
                "lineage": _lineage(t, sel), "plan": plan, "checks": [], "sql": [], "measures": mis}
    if len(sel) == 0:
        raise PlanError("No rows match these filters" + (f" in {period['label']}" if period else "") + ".")
    keys = list(plan.get("group", []))
    work = sel.copy()
    tkey = ts.loc[sel.index] if ts is not None else None
    if grain:
        work["__bucket"] = bucket(ts.loc[sel.index], month.loc[sel.index], grain)
        keys = ["__bucket"] + keys
    avg_b = None
    for mi in mis:
        if mi["agg"] == "avg_per" and ts is not None:
            avg_b = bucket(ts.loc[sel.index], month.loc[sel.index], mi.get("grain") or "month")
    res = None
    for i, mi in enumerate(mis):
        r = aggregate(work, keys, mi, tkey, avg_b).rename(f"m{i}")
        res = r.to_frame() if res is None else res.join(r, how="outer")
    res = res.reset_index()
    if not keys:
        res = res.drop(columns=["__all__"], errors="ignore")
    # ---- SQL cross-check (independent)
    checks, sqls = [], []
    try:
        reg = df.copy()
        for c in reg.columns:
            if reg[c].dtype == object:
                reg[c] = reg[c].map(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x))
        where = sql_where(plan, t, period, reg)
        keys_sql = ([("__bucket", sql_bucket(t, grain))] if grain else []) + [(g, q(g)) for g in plan.get("group", [])]
        with _DLOCK:
            con = duckdb.connect()
            con.register("t", reg)
            for i, mi in enumerate(mis):
                sql = sql_measure(t, mi, keys_sql, where, sql_bucket(t, mi.get("grain") or "month") if mi["agg"] == "avg_per" else None)
                sqls.append(sql)
                sdf = con.execute(sql).df()
                checks.append(_compare(res, sdf, [a for a, _ in keys_sql], f"m{i}", mi["text"]))
            con.close()
    except Exception as ex:  # a failed cross-check is reported, never hidden
        checks.append({"label": "SQL cross-check", "match": False, "n": 0, "matched": 0, "error": str(ex)[:200]})
    out = {"mode": "aggregate", "table": t, "res": res, "keys": keys, "group": plan.get("group", []), "grain": grain, "measures": mis, "period": period,
           "rows_used": int(len(sel)), "lineage": _lineage(t, sel), "notes": notes, "checks": checks, "sql": sqls, "plan": plan}
    # ---- comparison
    if plan.get("compare") and period:
        pp = shift_period(period, plan["compare"])
        pmask = period_mask(pp, t, ts, month)
        psel = df[base_mask & pmask]
        out["prev_period"] = pp
        if len(psel):
            pw = psel.copy()
            if grain:
                bts = ts.loc[psel.index]
                if plan["compare"] == "previous_year":
                    bts = bts + pd.DateOffset(years=1)
                else:
                    n = len(pd.period_range(period["start"], period["end"], freq="M")) if not period.get("d0") else None
                    bts = bts + (pd.DateOffset(months=n) if n else pd.Timedelta(days=(pd.Timestamp(period["d1"]) - pd.Timestamp(period["d0"])).days + 1))
                pw["__bucket"] = bucket(bts, bts.dt.strftime("%Y-%m"), grain)
            prev = None
            for i, mi in enumerate(mis):
                r = aggregate(pw, keys, mi, ts.loc[psel.index], bucket(ts.loc[psel.index], month.loc[psel.index], mi.get("grain") or "month") if mi["agg"] == "avg_per" else None).rename(f"p{i}")
                prev = r.to_frame() if prev is None else prev.join(r, how="outer")
            prev = prev.reset_index()
            if not keys:
                prev = prev.drop(columns=["__all__"], errors="ignore")
                res = pd.concat([res, prev], axis=1)
            else:
                res = res.merge(prev, on=keys, how="left")
            for i in range(len(mis)):
                res[f"c{i}"] = res[f"m{i}"] - res[f"p{i}"]
                res[f"g{i}"] = np.where(res[f"p{i}"].abs() > 0, res[f"c{i}"] / res[f"p{i}"].abs() * 100, np.nan)
            out["rows_used_prev"] = int(len(psel))
        else:
            notes.append(f"No data for the comparison period ({pp['label']}).")
    # ---- having, share, sort, limit
    for h in plan.get("having", []):
        idx = next((i for i, mi in enumerate(mis) if mi["col"] == h["col"]), None)
        col = f"m{idx}" if idx is not None else None
        if col is None:
            mi = measure_info(t, {"col": h["col"]})
            r = aggregate(work, keys, mi, tkey).rename("__h").reset_index()
            res = res.merge(r, on=keys, how="left") if keys else res.assign(__h=r["__h"].iloc[0])
            col = "__h"
        v = h["value"]
        res = res[{">": res[col] > v, ">=": res[col] >= v, "<": res[col] < v, "<=": res[col] <= v}[h["op"]]]
        if col == "__h":
            res = res.drop(columns=["__h"])
    additive = mis[0]["agg"] in ("sum", "count", "count_distinct", "last")
    if plan.get("share") and keys and additive:
        tot = res["m0"].sum()
        res["s0"] = res["m0"] / tot * 100 if tot else np.nan
    elif plan.get("share") and not additive:
        notes.append(f"A share of total is not meaningful for {mis[0]['text'].lower()} (it is an average/rate), so values are shown instead.")
    if grain and not plan.get("group"):
        if plan.get("limit"):
            res = res.sort_values("m0", ascending=plan.get("sort") == "asc", na_position="last").head(int(plan["limit"]))
        res = res.sort_values("__bucket")
    elif grain:
        res = res.sort_values(["__bucket", "m0"], ascending=[True, plan.get("sort") == "asc"])
    elif keys and (plan.get("sort") or plan.get("group")):
        res = res.sort_values("m0", ascending=plan.get("sort") == "asc", na_position="last")
        if plan.get("limit"):
            res = res.head(int(plan["limit"]))
    if grain in ("month", "quarter", "year", "fy") and t.time_type in ("date", "datetime") and ts is not None and ts.notna().any():
        last = ts.max()
        if last.day < (last + pd.offsets.MonthEnd(0)).day - 2 and (period is None or period.get("end", "") >= last.strftime("%Y-%m")):
            notes.append(f"The latest {grain} is incomplete: data runs only to {last:%d-%b-%Y}.")
    out["res"] = res.reset_index(drop=True)
    out["notes"] = notes
    return out


def _compare(res: pd.DataFrame, sdf: pd.DataFrame, keys: list[str], col: str, label: str) -> dict:
    a = res[keys + [col]].copy() if keys else res[[col]].copy()
    b = sdf.rename(columns={"v": "__sql"})
    if keys:
        for k in keys:
            a[k] = a[k].astype(str)
            b[k] = b[k].astype(str)
        m = a.merge(b, on=keys, how="outer")
    else:
        m = pd.concat([a.reset_index(drop=True), b.reset_index(drop=True)], axis=1)
    x, y = m[col].astype(float), m["__sql"].astype(float)
    both_nan = x.isna() & y.isna()
    ok = both_nan | ((x - y).abs() <= 1e-6 * np.maximum(1.0, np.maximum(x.abs(), y.abs())))
    first = (float(x.iloc[0]) if len(x) and pd.notna(x.iloc[0]) else None, float(y.iloc[0]) if len(y) and pd.notna(y.iloc[0]) else None)
    return {"label": label, "n": int(len(m)), "matched": int(ok.sum()), "match": bool(ok.all()), "primary": first[0], "crosscheck": first[1]}


def _lineage(t, sel: pd.DataFrame) -> dict:
    if t.lineage is None:
        return {}
    try:
        return {str(k): int(v) for k, v in t.lineage.reindex(sel.index).value_counts().items()}
    except Exception:
        return {}
