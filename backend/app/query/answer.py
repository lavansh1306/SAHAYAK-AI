"""Turn an executed plan into a user-facing answer: plain-language lines, headline numbers, chart, table,
sources (file + filter + rows + formula), cross-check stamp, the plan in words and follow-up suggestions."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..util import decimals_for, fmt_value, group, nice_label, pct

GRAIN_NAME = {"hour": "hour", "day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year", "fy": "financial year"}


def result(kind: str, title: str, answer: list[str], **kw) -> dict:
    r = {"type": kw.pop("type", "answer"), "intent": kind, "title": title, "answer": answer, "kpis": [], "table": None, "tables": [], "chart": None,
         "charts": [], "sources": [], "checks": [], "notes": [], "assumptions": [], "followups": [], "files": [], "verified": True}
    r.update(kw)
    return r


def _fmt(v, mi, d=None):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return fmt_value(float(v), mi.get("unit", ""), mi.get("unit_kind", ""), d)


def _decs(series, mi):
    if mi.get("agg") in ("count", "count_distinct"):
        return 0
    if mi.get("unit_kind") == "percent" or mi.get("agg") in ("ratio", "mean") and mi.get("unit_kind") in ("percent", "money", ""):
        return 2
    return decimals_for(list(series), mi.get("unit_kind", ""))


def _col_label(t, c: str) -> str:
    if c == "__bucket":
        return "Period"
    e = t.cols.get(c)
    if e:
        return e.get("label") or nice_label(c)
    return nice_label(c)


def filter_text(t, plan: dict) -> str:
    parts = []
    for f in plan.get("filters", []):
        lab = _col_label(t, f["col"])
        if f["op"] == "in":
            parts.append(f"{lab} = {', '.join(map(str, f['values']))}")
        elif f["op"] == "not_in":
            parts.append(f"{lab} not {', '.join(map(str, f['values']))}")
        else:
            parts.append(f"{lab} {f['op']} {group(float(f['values'][0]), 2).rstrip('0').rstrip('.') if isinstance(f['values'][0], (int, float)) else f['values'][0]}")
    return "; ".join(parts)


def plan_words(t, x: dict) -> list[str]:
    p = x["plan"]
    steps = [f"Table: {t.label}"]
    steps.append("Measure: " + "; ".join(mi["text"] + (f" (= total {mi['ratio']['num']} / total {mi['ratio']['den']}" + (f" x {mi['ratio']['factor']:g}" if mi['ratio']['factor'] != 1 else "") + ")" if mi["agg"] == "ratio" and mi.get("ratio") else "") for mi in x["measures"]))
    ft = filter_text(t, p)
    if ft:
        steps.append(f"Filter: {ft}")
    if x.get("period"):
        steps.append(f"Period: {x['period']['label']}")
    g = ([f"each {GRAIN_NAME.get(x['grain'], x['grain'])}"] if x.get("grain") else []) + [_col_label(t, c) for c in x.get("group", [])]
    if g:
        steps.append("Grouped by: " + ", ".join(g))
    if p.get("compare") and x.get("prev_period"):
        steps.append(f"Compared with: {x['prev_period']['label']}")
    if p.get("having"):
        steps.append("Kept only: " + "; ".join(f"{h['col']} {h['op']} {h['value']:g}" for h in p["having"]))
    return steps


def sources(t, x: dict) -> list[dict]:
    lin = x.get("lineage") or {}
    files = ", ".join(f"{k} ({group(v)} rows)" for k, v in list(lin.items())[:3]) or ", ".join(t.entry.get("sources", [])[:3])
    ft = filter_text(t, x["plan"])
    if x.get("period"):
        ft = (ft + "; " if ft else "") + x["period"]["label"]
    return [{"file": files, "origin": t.label, "filter": ft, "rows_used": x.get("rows_used", x.get("total")), "formula": (x.get("sql") or [""])[0]}]


def followups(t, x: dict, store) -> list[str]:
    out = []
    mi = x["measures"][0]
    m = mi["label"].split(" (")[0].lower()
    dims = [d for d in t.dims() if d not in x.get("group", [])]
    if dims and not x.get("group"):
        out.append(f"{mi['label'].split(' (')[0]} by {_col_label(t, dims[0]).lower()}")
    if t.time and not x.get("grain"):
        out.append(f"Monthly trend of {m}")
    if t.time and not x["plan"].get("compare") and t.time_type in ("month", "date", "datetime"):
        out.append(f"How did {m} change compared with last year?")
    if x.get("group") and not x["plan"].get("share") and mi["agg"] in ("sum", "count"):
        out.append(f"Share of {m} by {_col_label(t, x['group'][0]).lower()} as a pie chart")
    return out[:3]


def compose(store, x: dict, question: str) -> dict:
    t = x["table"]
    if x["mode"] == "rows":
        rows = x["rows"]
        cols = list(rows.columns)
        tbl = {"columns": [_col_label(t, c) for c in cols], "rows": [[_cell(v) for v in r] for r in rows.astype(object).values.tolist()],
               "caption": f"{group(x['total'])} matching row(s)" + (f" - first {len(rows)} shown" if x["total"] > len(rows) else ""), "total_rows": int(x["total"])}
        ans = [f"**{group(x['total'])} row(s)** in {t.label} match" + (f" ({filter_text(t, x['plan'])})" if filter_text(t, x["plan"]) else "") + (f", {x['period']['label']}" if x.get("period") else "") + "."]
        return result("rows", f"{t.label}: matching rows", ans, table=tbl, sources=sources(t, x), notes=x["notes"], plan_text=plan_words(t, x),
                      followups=followups(t, {**x, "group": []}, store))
    res: pd.DataFrame = x["res"]
    mis = x["measures"]
    m0 = mis[0]
    keys, grain, grp = x["keys"], x.get("grain"), x.get("group", [])
    per = x.get("period")
    plab = per["label"] if per else _span(t)
    dec = _decs(res["m0"].dropna(), m0) if len(res) else 2
    answer, kpis, chart, table = [], [], None, None
    cmp_ = "p0" in res.columns
    title = m0["text"] + (f" by {', '.join(([GRAIN_NAME.get(grain, grain)] if grain else []) + [_col_label(t, g).lower() for g in grp])}" if keys else "") + f" - {plab}"
    ft = filter_text(t, x["plan"])
    ftxt = f" ({ft})" if ft else ""
    if not keys:
        v = res["m0"].iloc[0] if len(res) else np.nan
        answer.append(f"**{m0['text']}{ftxt}, {plab}: {_fmt(v, m0, dec)}**")
        kpis.append(_kpi(m0["text"], v, m0, dec))
        for i, mi in enumerate(mis[1:], 1):
            vi = res[f"m{i}"].iloc[0]
            answer.append(f"{mi['text']}: **{_fmt(vi, mi)}**")
            kpis.append(_kpi(mi["text"], vi, mi))
        if cmp_:
            pv, g = res["p0"].iloc[0], res["g0"].iloc[0]
            pl = x["prev_period"]["label"]
            if pd.notna(pv):
                direction = "up" if res["c0"].iloc[0] > 0 else "down" if res["c0"].iloc[0] < 0 else "unchanged"
                answer.append(f"{pl}: {_fmt(pv, m0, dec)} -> **{direction} {pct(abs(g), 1, False) if pd.notna(g) else ''}** ({'+' if res['c0'].iloc[0] >= 0 else ''}{_fmt(res['c0'].iloc[0], m0, dec)}).")
                kpis.append(_kpi(f"vs {pl}", g, {"unit_kind": "percent"}, 1, tone="good" if (g or 0) >= 0 else "bad", signed=True))
        table = None
    elif grain and not grp:
        s = res.dropna(subset=["m0"])
        xs = s["__bucket"].astype(str).tolist()
        ys = s["m0"].tolist()
        series = [{"name": m0["text"], "data": [_r(v, dec) for v in ys]}]
        if cmp_:
            series.append({"name": f"{m0['text']} ({x['prev_period']['label']})", "data": [_r(v, dec) for v in s["p0"].tolist()]})
        for i, mi in enumerate(mis[1:], 1):
            series.append({"name": mi["text"], "data": [_r(v, 2) for v in s[f"m{i}"].tolist()], "axis": "right"})
        kind = x["plan"].get("chart") if x["plan"].get("chart") in ("bar", "line") else ("line" if len(xs) > 3 else "bar")
        chart = {"type": kind, "title": title, "x": xs, "series": series, "unit": m0.get("unit", "")}
        if len(ys):
            i_max, i_min = int(np.nanargmax(ys)), int(np.nanargmin(ys))
            first, last = ys[0], ys[-1]
            ch = (last / first - 1) * 100 if first else np.nan
            if x["plan"].get("sort") and not cmp_:
                srt = s.sort_values("m0", ascending=x["plan"]["sort"] == "asc")
                answer.append(f"**{'Lowest' if x['plan']['sort'] == 'asc' else 'Highest'}: {srt['__bucket'].iloc[0]}** with {_fmt(srt['m0'].iloc[0], m0, dec)}{ftxt}"
                              + (f"; next {srt['__bucket'].iloc[1]} ({_fmt(srt['m0'].iloc[1], m0, dec)})." if len(srt) > 1 else "."))
            answer.append(f"{m0['text']}{ftxt} went from {_fmt(first, m0, dec)} ({xs[0]}) to {_fmt(last, m0, dec)} ({xs[-1]})" + (f", **{pct(ch)}**." if pd.notna(ch) and len(xs) > 1 else "."))
            answer.append(f"Highest: {xs[i_max]} ({_fmt(ys[i_max], m0, dec)}). Lowest: {xs[i_min]} ({_fmt(ys[i_min], m0, dec)}).")
            if m0["agg"] in ("sum", "count") and len(ys) > 1:
                answer.append(f"Average per {GRAIN_NAME.get(grain, grain)}: {_fmt(float(np.nanmean(ys)), m0, dec)}; total {_fmt(float(np.nansum(ys)), m0, dec)}.")
            if cmp_:
                tot_c, tot_p = float(np.nansum(s["m0"])), float(np.nansum(s["p0"]))
                if tot_p and m0["agg"] in ("sum", "count"):
                    answer.append(f"Compared with {x['prev_period']['label']}: {_fmt(tot_p, m0, dec)} -> {_fmt(tot_c, m0, dec)} (**{pct((tot_c / tot_p - 1) * 100)}**).")
            kpis = [_kpi(f"{xs[-1]}", last, m0, dec), _kpi(f"Highest ({xs[i_max]})", ys[i_max], m0, dec), _kpi(f"Lowest ({xs[i_min]})", ys[i_min], m0, dec)]
        table = _table(t, res, keys, mis, dec, cmp_, x)
    else:
        lab_col = grp[0] if grp else "__bucket"
        s = res.dropna(subset=["m0"])
        if grain and grp:
            # one line per member
            piv = s.pivot_table(index="__bucket", columns=grp[0], values="m0", aggfunc="first").sort_index()
            order = piv.sum().sort_values(ascending=False).index.tolist()
            top = order[:8]
            chart = {"type": "line" if len(piv) > 3 else "bar", "title": title, "x": [str(i) for i in piv.index],
                     "series": [{"name": str(c), "data": [_r(v, dec) for v in piv[c].tolist()]} for c in top], "unit": m0.get("unit", "")}
            last_b = piv.index[-1]
            lastv = piv.loc[last_b].dropna().sort_values(ascending=False)
            if len(lastv):
                answer.append(f"In {last_b}: highest **{lastv.index[0]}** ({_fmt(lastv.iloc[0], m0, dec)}), lowest {lastv.index[-1]} ({_fmt(lastv.iloc[-1], m0, dec)}).")
            for c in top[:3]:
                col = piv[c].dropna()
                if len(col) > 1 and col.iloc[0]:
                    answer.append(f"{c}: {_fmt(col.iloc[0], m0, dec)} -> {_fmt(col.iloc[-1], m0, dec)} ({pct((col.iloc[-1] / col.iloc[0] - 1) * 100)}) from {col.index[0]} to {col.index[-1]}.")
            if len(order) > 8:
                answer.append(f"Chart shows the top 8 of {len(order)}; all are in the table.")
        else:
            labels = s[lab_col].astype(str).tolist()
            vals = s["m0"].tolist()
            n = len(labels)
            if n:
                srt = x["plan"].get("sort") or "desc"
                word = "lowest" if srt == "asc" else "highest"
                sh = f" ({s['s0'].iloc[0]:.1f}% of total)" if "s0" in s.columns and pd.notna(s["s0"].iloc[0]) else ""
                answer.append(f"**{labels[0]}** has the {word} {_lc(m0['text'])}{ftxt}: **{_fmt(vals[0], m0, dec)}**{sh}, {plab}.")
                lim = x["plan"].get("limit")
                if n > 1 and lim:
                    answer.append(f"{'Bottom' if srt == 'asc' else 'Top'} {n}: " + ", ".join(f"{a} ({_fmt(b, m0, dec)})" for a, b in zip(labels, vals)) + ".")
                elif n > 1:
                    answer.append("Next: " + ", ".join(f"{a} ({_fmt(b, m0, dec)})" for a, b in list(zip(labels, vals))[1:4]) + ".")
                    other = "highest" if srt == "asc" else "lowest"
                    answer.append(f"{other.capitalize()}: {labels[-1]} ({_fmt(vals[-1], m0, dec)})" + (f"; {n} {_col_label(t, lab_col).lower()} values in total." if n > 4 else "."))
                if m0["agg"] in ("sum", "count", "count_distinct", "last") and n > 1 and not lim:
                    tot = float(np.nansum(vals))
                    answer.append(f"Total across all {n}: {_fmt(tot, m0, dec)}.")
                elif m0["agg"] in ("ratio", "mean") and n > 1:
                    ov = _overall(store, x)
                    if ov is not None:
                        answer.append(f"Overall (all together): {_fmt(ov, m0, dec)}.")
                if cmp_:
                    sc = s.dropna(subset=["g0"]).sort_values("g0")
                    if len(sc):
                        answer.append(f"Vs {x['prev_period']['label']}: biggest rise {sc[lab_col].iloc[-1]} ({pct(sc['g0'].iloc[-1])}), biggest fall/smallest rise {sc[lab_col].iloc[0]} ({pct(sc['g0'].iloc[0])}).")
                for i, mi in enumerate(mis[1:], 1):
                    answer.append(f"{mi['text']} for {labels[0]}: {_fmt(s[f'm{i}'].iloc[0], mi)}.")
                kind = x["plan"].get("chart")
                if kind not in ("bar", "line", "pie", "table"):
                    kind = "pie" if (x["plan"].get("share") and "s0" in s.columns and n <= 10) else "bar"
                if kind == "pie" and "s0" not in s.columns:
                    kind = "bar"
                if kind != "table":
                    series = [{"name": m0["text"], "data": [_r(v, dec) for v in vals]}]
                    if cmp_:
                        series.append({"name": x["prev_period"]["label"], "data": [_r(v, dec) for v in s["p0"].tolist()]})
                    chart = {"type": kind, "title": title, "x": labels[:60], "series": [{**se, "data": se["data"][:60]} for se in series], "unit": m0.get("unit", ""),
                             "highlight": [labels[0]] if kind == "bar" and not cmp_ else None}
                kpis = [_kpi(f"{word.capitalize()}: {labels[0]}", vals[0], m0, dec)]
                if n > 1:
                    kpis.append(_kpi(f"{'Highest' if srt == 'asc' else 'Lowest'}: {labels[-1]}", vals[-1], m0, dec))
                if m0["agg"] in ("sum", "count", "count_distinct", "last") and n > 1:
                    kpis.append(_kpi("Total", float(np.nansum(vals)), m0, dec))
        table = _table(t, res, keys, mis, dec, cmp_, x)
    checks = x.get("checks", [])
    ok = all(c.get("match") for c in checks) and bool(checks)
    notes = list(x.get("notes", []))
    if not per and t.time:
        notes.append(f"No period was given, so all available data ({_span(t)}) was used.")
    for c, e in t.cols.items():
        if e.get("type") == "fy_label" and e.get("mismatch") and ("fy" in question.lower() or "financial" in question.lower()):
            notes.append(f"The file's own '{c}' column labels some rows with a different financial year. Financial years here are worked out from '{t.time}' (starting in month {_fy_start()}).")
    r = result("query", title, answer, kpis=kpis[:4], chart=chart, table=table, sources=sources(t, x), checks=checks, notes=notes,
               plan_text=plan_words(t, x), followups=followups(t, x, store), verified=ok, facts=_facts(res, mis))
    return r


def _lc(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _fy_start():
    from ..config import fy_start
    return fy_start()


def _overall(store, x: dict):
    from . import engine
    p = {**x["plan"], "group": [], "grain": None, "compare": None, "having": [], "limit": None, "share": False, "sort": None}
    try:
        y = engine.run(store, p)
        return float(y["res"]["m0"].iloc[0])
    except Exception:
        return None


def _span(t) -> str:
    tr = t.entry.get("time_range")
    if not tr:
        return "all rows"
    a, b = str(tr[0])[:10], str(tr[1])[:10]
    if t.time_type == "month":
        from .periods import describe
        return describe(a[:7], b[:7])
    try:
        return f"{pd.Timestamp(a):%d-%b-%Y} to {pd.Timestamp(b):%d-%b-%Y}"
    except Exception:
        return f"{a} to {b}"


def _r(v, d):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), max(d, 2))


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, (np.floating, float)):
        return round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    return v


def _kpi(label, v, mi, d=None, tone="", signed=False):
    if signed:
        disp = pct(v, 1) if v is not None and not (isinstance(v, float) and math.isnan(v)) else "-"
    else:
        disp = _fmt(v, mi, d)
    return {"label": label, "value": None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v), "display": disp, "tone": tone}


def _table(t, res, keys, mis, dec, cmp_, x) -> dict:
    cols, rows = [], []
    kc = [k for k in keys]
    cols = [_col_label(t, k) for k in kc]
    for i, mi in enumerate(mis):
        cols.append(mi["text"] + (f" ({mi['unit']})" if mi.get("unit") and mi["unit"] not in mi["text"] else ""))
        if "s0" in res.columns and i == 0:
            cols.append("Share %")
        if cmp_:
            cols += [f"{x['prev_period']['label']}", "Change", "Change %"]
    for _, r in res.iterrows():
        row = [str(r[k]) for k in kc]
        for i, mi in enumerate(mis):
            d = dec if i == 0 else 2
            row.append(_r(r[f"m{i}"], d))
            if "s0" in res.columns and i == 0:
                row.append(_r(r["s0"], 1))
            if cmp_:
                row += [_r(r.get(f"p{i}"), d), _r(r.get(f"c{i}"), d), _r(r.get(f"g{i}"), 1)]
        rows.append(row)
    return {"columns": cols, "rows": rows[:500], "caption": f"{len(rows)} row(s)", "total_rows": len(rows)}


def _facts(res: pd.DataFrame, mis) -> dict:
    f = {}
    for c in res.columns:
        if c.startswith(("m", "p", "c", "g", "s")) and c[1:].isdigit():
            vals = res[c].dropna().tolist()[:50]
            f[c] = vals
    return f
