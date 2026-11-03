"""Reports from whatever is loaded: MIS-style PDF + Word, a one-page infographic, and edits by request
("redo it in crore", "add a section on ...", "only for <area>"). Every figure comes from the query engine and carries
its source; tables and figures are numbered automatically."""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd

from ..config import OUTPUT_DIR, app_settings
from ..query import engine as E
from ..query.answer import compose, result
from ..query.periods import add_months, describe, fy_label, fy_start_year, month_long, parse_period, today
from ..query.understand import find_values, normalise, rules_plan
from ..util import fmt_value, group, pct, slug
from . import charts
from .docmodel import to_docx, to_pdf

MONEY = {"Rs": 1.0, "Rs thousand": 1e3, "Rs lakh": 1e5, "Rs million": 1e6, "Rs crore": 1e7}


def money_conv(mi: dict, unit: str | None) -> tuple[float, str]:
    """Factor to multiply a value by, and the new unit label, to show money in lakh or crore."""
    u = mi.get("unit") or ""
    if not unit or mi.get("unit_kind") != "money" or u not in MONEY or mi.get("agg") in ("ratio", "mean"):
        return 1.0, u
    target = {"lakh": "Rs lakh", "crore": "Rs crore"}[unit]
    return MONEY[u] / MONEY[target], target


def _fmtv(v, mi, unit=None, d=None):
    f, u = money_conv(mi, unit)
    return fmt_value(None if v is None or (isinstance(v, float) and np.isnan(v)) else v * f, u, mi.get("unit_kind", ""), d)


def _plan(t, measures, **kw):
    p = {"table": t.key, "measures": measures, "group": [], "grain": None, "filters": [], "having": [], "period": None, "compare": None, "sort": None,
         "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
    p.update(kw)
    return p


def _filters_for(t, flt: dict) -> list[dict]:
    return [{"col": c, "op": "in", "values": v} for c, v in (flt or {}).items() if c in t.df.columns]


def _src(t) -> str:
    return f"{t.label} ({', '.join(t.entry.get('sources', [])[:2])})"


def _report_month(store, tables, spec) -> str | None:
    if spec.get("period_end"):
        return spec["period_end"]
    ms = []
    for t in tables:
        tr = t.entry.get("time_range")
        if tr and t.time_type in ("month", "date", "datetime"):
            last = str(tr[1])[:7]
            if t.time_type in ("date", "datetime"):
                d = pd.Timestamp(str(tr[1])[:10])
                if d.day < (d + pd.offsets.MonthEnd(0)).day - 2:
                    last = add_months(last, -1)
            ms.append(last)
    return max(ms) if ms else None


def _table_section(store, t, M: str | None, spec, n: int, figs: list, prov: list) -> list[dict]:
    unit = spec.get("unit")
    flt = _filters_for(t, spec.get("filters"))
    ms = t.measures()[:3]
    dims = [d for d in t.dims() if d not in (spec.get("filters") or {}) and t.cols[d].get("type") != "fy_label" and d != "unit_reported"]
    blocks = [{"t": "h1", "text": f"{n}. {t.label}"}]
    if not ms:
        if dims:
            x = E.run(store, _plan(t, [{"col": "*", "agg": "count"}], group=[dims[0]], filters=flt, share=True))
            r = x["res"]
            blocks.append({"t": "p", "text": f"{group(int(r['m0'].sum()))} records in total; most in **{r[dims[0]].iloc[0]}** ({r['m0'].iloc[0]:,.0f})."})
            blocks.append({"t": "table", "caption": f"{t.label} by {t.cols[dims[0]]['label'].lower()}", "columns": [t.cols[dims[0]]["label"], "Records", "Share %"],
                           "rows": [[str(a), group(b), f"{c:.1f}"] for a, b, c in zip(r[dims[0]], r["m0"], r["s0"])][:25], "source": _src(t)})
        return blocks
    m0 = ms[0]
    has_month = t.time_type in ("month", "date", "datetime") and M is not None
    lines = []
    if has_month:
        S = app_settings()["fiscal_year_start_month"]
        fy0 = fy_start_year(M)
        start = f"{fy0:04d}-{S:02d}" if S > 1 else f"{fy0:04d}-01"
        for c in ms:
            try:
                xm = E.run(store, _plan(t, [{"col": c}], filters=flt, period={"start": M, "end": M, "label": month_long(M), "kind": "month"}, compare="previous_year"))
                mi = xm["measures"][0]
                v = xm["res"]["m0"].iloc[0]
                g = xm["res"]["g0"].iloc[0] if "g0" in xm["res"].columns else np.nan
                line = f"{mi['text']} for {month_long(M)}: **{_fmtv(v, mi, unit)}**" + (f" ({pct(g)} vs {xm['prev_period']['label']})" if pd.notna(g) else "")
                if mi["agg"] not in ("last",) and start < M:
                    xf = E.run(store, _plan(t, [{"col": c}], filters=flt, period={"start": start, "end": M, "label": describe(start, M), "kind": "range"}, compare="previous_year"))
                    vf = xf["res"]["m0"].iloc[0]
                    gf = xf["res"]["g0"].iloc[0] if "g0" in xf["res"].columns else np.nan
                    line += f"; {describe(start, M)} ({fy_label(fy0)} to date): **{_fmtv(vf, mi, unit)}**" + (f" ({pct(gf)})" if pd.notna(gf) else "")
                    prov.append((f"{t.label}: {mi['text']} {describe(start, M)}", _fmtv(vf, mi, unit), _src(t), (xf.get("sql") or [""])[0][:160]))
                lines.append(line + ".")
                prov.append((f"{t.label}: {mi['text']} {month_long(M)}", _fmtv(v, mi, unit), _src(t), (xm.get("sql") or [""])[0][:160]))
            except E.PlanError:
                continue
    else:
        for c in ms:
            try:
                x = E.run(store, _plan(t, [{"col": c}], filters=flt))
                mi = x["measures"][0]
                lines.append(f"{mi['text']} ({x['period']['label'] if x.get('period') else 'all data'}): **{_fmtv(x['res']['m0'].iloc[0], mi, unit)}**.")
            except E.PlanError:
                continue
    if lines:
        blocks.append({"t": "bullets", "items": lines})
    # breakdown by main grouping
    if dims:
        d = dims[0]
        per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if has_month else None
        try:
            x = E.run(store, _plan(t, [{"col": m0}], group=[d], filters=flt, period=per, share=True, compare="previous_year" if has_month else None, sort="desc"))
            r = x["res"]
            mi = x["measures"][0]
            f, u = money_conv(mi, unit)
            cols = [t.cols[d]["label"], f"{mi['text']}" + (f" ({u})" if u and u != "%" else "")]
            if "s0" in r.columns:
                cols.append("Share %")
            if "p0" in r.columns:
                cols += [x["prev_period"]["label"], "Change %"]
            rows, hl = [], []
            for i, rr in r.head(25).iterrows():
                row = [str(rr[d]), group(rr["m0"] * f, 2)]
                if "s0" in r.columns:
                    row.append(f"{rr['s0']:.1f}")
                if "p0" in r.columns:
                    row += [group(rr["p0"] * f, 2) if pd.notna(rr["p0"]) else "-", pct(rr["g0"]) if pd.notna(rr.get("g0")) else "-"]
                rows.append(row)
            blocks.append({"t": "table", "caption": f"{mi['text']} by {t.cols[d]['label'].lower()}, {x['period']['label'] if x.get('period') else 'all data'}",
                           "columns": cols, "rows": rows, "source": _src(t)})
            top = r.head(12)
            p = charts.bar([str(v) for v in top[d]], [float(v) * f for v in top["m0"]], title=f"{mi['text']} by {t.cols[d]['label'].lower()}", ylabel=u, highlight=[str(top[d].iloc[0])],
                           fmt="{:,.0f}" if top["m0"].abs().max() * f >= 100 else "{:,.2f}")
            blocks.append({"t": "image", "path": str(p), "caption": f"{mi['text']} by {t.cols[d]['label'].lower()}", "source": _src(t)})
            figs.append(p)
        except E.PlanError:
            pass
    # trend
    if t.time and t.entry.get("grain") != "none":
        g = "month" if has_month else "day"
        per = {"start": add_months(M, -23), "end": M, "label": describe(add_months(M, -23), M), "kind": "range"} if has_month else None
        try:
            x = E.run(store, _plan(t, [{"col": m0}], grain=g, filters=flt, period=per))
            r = x["res"]
            mi = x["measures"][0]
            f, u = money_conv(mi, unit)
            if len(r) >= 3:
                p = charts.line([str(v) for v in r["__bucket"]], {mi["text"]: [float(v) * f for v in r["m0"]]}, title=f"{mi['text']} per {g}", ylabel=u)
                blocks.append({"t": "image", "path": str(p), "caption": f"{mi['text']} per {g}, {x['period']['label'] if x.get('period') else 'all data'}", "source": _src(t)})
        except E.PlanError:
            pass
    # findings for this table
    try:
        from ..insights.engine import run_insights
        fs = [f_ for f_ in run_insights(store) if f_["table"] == t.key and f_["category"] != "data_quality"][:4]
        if fs:
            blocks.append({"t": "h2", "text": "What stands out"})
            blocks.append({"t": "bullets", "items": [f"**{f_['title']}** - {f_['detail'][0]}" for f_ in fs]})
    except Exception:
        pass
    return blocks


def _question_section(store, q: str, n: int, unit, flt) -> list[dict]:
    from ..query.router import ask
    r = ask(q, f"report-{time.time()}")
    blocks = [{"t": "h1", "text": f"{n}. {q[:1].upper() + q[1:]}"}]
    blocks.append({"t": "bullets", "items": [a for a in r.get("answer", [])][:6]})
    tb = r.get("table")
    if tb and tb.get("rows"):
        blocks.append({"t": "table", "caption": r.get("title", q), "columns": tb["columns"], "rows": [[("" if v is None else (group(v, 2) if isinstance(v, float) else v)) for v in row] for row in tb["rows"][:25]],
                       "source": (r.get("sources") or [{}])[0].get("file", "")})
    ch = r.get("chart")
    if ch and ch.get("x"):
        try:
            if ch["type"] == "pie":
                p = charts.pie(ch["x"][:8], ch["series"][0]["data"][:8], title=ch.get("title", "")[:70])
            elif ch["type"] == "line":
                p = charts.line(ch["x"], {s["name"][:40]: [v if v is not None else np.nan for v in s["data"]] for s in ch["series"][:4]}, title=ch.get("title", "")[:70])
            else:
                p = charts.bar(ch["x"][:15], [v or 0 for v in ch["series"][0]["data"][:15]], title=ch.get("title", "")[:70])
            blocks.append({"t": "image", "path": str(p), "caption": ch.get("title", q)[:90], "source": (r.get("sources") or [{}])[0].get("file", "")})
        except Exception:
            pass
    return blocks


def build(store, spec: dict) -> dict:
    t0 = time.time()
    spec = copy.deepcopy(spec)
    tables = [store.get(k) for k in (spec.get("tables") or [])] or [t for t in store.usable()]
    tables = [t for t in tables if t is not None]
    if spec.get("filters"):
        tables = [t for t in tables if any(c in t.df.columns for c in spec["filters"])] or tables
    if not tables:
        return result("report", "Nothing to report", ["No usable tables are loaded."], type="clarify")
    tables = sorted(tables, key=lambda t: -t.importance())[: spec.get("max_tables", 7)]
    M = _report_month(store, tables, spec)
    cs = app_settings()
    company = cs.get("company", "")
    ftxt = "; ".join(f"{c} = {', '.join(map(str, v))}" for c, v in (spec.get("filters") or {}).items())
    title = spec.get("title") or f"{company} - {'Monthly MIS report' if M else 'Data report'}{', ' + month_long(M) if M else ''}" + (f" ({ftxt})" if ftxt else "")
    unit = spec.get("unit") or None
    blocks = [{"t": "title", "text": title},
              {"t": "subtitle", "text": f"Prepared by SAHAYAK-AI on {datetime.now():%d-%b-%Y %H:%M} from {len(tables)} loaded table(s). "
                                        + (f"Money shown in Rs {unit}. " if unit else "") + "Every figure is computed from the source data named under each table and figure."}]
    figs, prov = [], []
    # headline tiles
    kp = []
    for t in tables:
        if not t.measures() or len(kp) >= 4:
            continue
        try:
            per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if M and t.time_type in ("month", "date", "datetime") else None
            x = E.run(store, _plan(t, [{"col": t.measures()[0]}], filters=_filters_for(t, spec.get("filters")), period=per, compare="previous_year" if per else None))
            mi = x["measures"][0]
            v = x["res"]["m0"].iloc[0]
            g = x["res"]["g0"].iloc[0] if "g0" in x["res"].columns else np.nan
            kp.append((f"{mi['text']}" + (f" ({pct(g, 1)} YoY)" if pd.notna(g) else ""), _fmtv(v, mi, unit)))
        except E.PlanError:
            continue
    if kp:
        blocks.append({"t": "kpis", "items": kp})
    sections = spec.get("include") or ["summary", "tables", "insights", "alerts", "quality"]
    n = 1
    if "summary" in sections:
        try:
            from ..insights.engine import run_insights
            fs = [f for f in run_insights(store) if f["table"] in {t.key for t in tables} and f["category"] != "data_quality"][:6]
        except Exception:
            fs = []
        if fs:
            blocks += [{"t": "h1", "text": f"{n}. Highlights and red flags"}, {"t": "bullets", "items": [f"**{f['title']}** - {f['detail'][0]}" for f in fs]}]
            n += 1
    if "tables" in sections:
        for t in tables:
            blocks += _table_section(store, t, M, spec, n, figs, prov)
            n += 1
    for q in spec.get("sections", []):
        blocks += _question_section(store, q, n, unit, spec.get("filters"))
        n += 1
    if "alerts" in sections:
        from ..alerts.engine import evaluate
        al = [a for a in evaluate(store) if a["table"] in {t.key for t in tables}][:15]
        if al:
            blocks += [{"t": "h1", "text": f"{n}. Alerts and action points"},
                       {"t": "table", "caption": "Current alerts", "columns": ["Severity", "Alert", "Item", "Period", "Value / evidence"],
                        "rows": [[a["severity"], a["alert"][:70], a["entity"], a["period"], (a["value"] or a["evidence"])[:90]] for a in al],
                        "highlight": [i for i, a in enumerate(al) if a["severity"] == "Red"], "source": "Alert rules and automatic checks"}]
            try:
                from ..decision.support import top_actions
                ta = top_actions(store)
                blocks.append({"t": "bullets", "items": [a for a in ta["answer"][1:6]]})
            except Exception:
                pass
            n += 1
    if "quality" in sections:
        rows = []
        for t in tables:
            warn = [c["check"] for c in t.entry.get("validation", []) if c["status"] != "pass"]
            rows.append([t.label, ", ".join(t.entry.get("sources", [])[:3]), group(len(t.df)), " to ".join(str(x)[:10] for x in (t.entry.get("time_range") or [])) or "-",
                         f"{t.entry.get('quality', 0):.0f}%", "; ".join(warn)[:120] or "none"])
        blocks += [{"t": "h1", "text": f"{n}. Data sources and quality"},
                   {"t": "table", "caption": "Tables used in this report", "columns": ["Table", "Files", "Rows", "Covers", "Quality", "Warnings"], "rows": rows, "source": "SAHAYAK-AI pipeline"}]
        if prov:
            blocks.append({"t": "table", "caption": "How the headline figures were computed", "columns": ["Figure", "Value", "Source", "Calculation"],
                           "rows": [list(p) for p in prov[:20]], "source": "Query engine (each value also cross-checked with an independent SQL query)"})
    base = slug(spec.get("name") or title, 60)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    files = []
    formats = spec.get("formats") or ["pdf", "docx"]
    head = f"{cs.get('company_short') or company} | SAHAYAK-AI"
    foot = "Figures computed from uploaded source data"
    if "pdf" in formats:
        p = to_pdf(blocks, OUTPUT_DIR / f"{base}_{stamp}.pdf", head, foot)
        files.append({"name": p.name, "label": "PDF report"})
    if "docx" in formats:
        p = to_docx(blocks, OUTPUT_DIR / f"{base}_{stamp}.docx", head, foot)
        files.append({"name": p.name, "label": "Word report (.docx)"})
    outline = [b["text"] for b in blocks if b["t"] == "h1"]
    ans = [f"Report ready in {time.time() - t0:.1f} s: **{title}**.", "Sections: " + "; ".join(outline) + "."]
    if unit:
        ans.append(f"Money is shown in Rs {unit} where the data unit is known.")
    out = result("report", "Report created", ans, files=files, outline=outline)
    out["report_spec"] = spec
    return out


def _period_from(q: str, spec: dict) -> None:
    p = parse_period(normalise(q), today())
    if p:
        spec["period_end"] = p.end if p.kind in ("month", "range", "fy", "h1", "h2", "quarter") and p.end != "9999-12" else spec.get("period_end")


def report_from_request(store, q: str) -> dict:
    qn = normalise(q)
    if re.search(r"\b(infographic|one[- ]?pager|poster|single page)\b", qn):
        return infographic(store, q)
    spec = {"formats": [f for f in ("pdf", "docx") if (f == "pdf" and "pdf" in qn) or (f == "docx" and re.search(r"\b(word|docx|doc)\b", qn))] or ["pdf", "docx"]}
    if re.search(r"\bcrores?\b", qn):
        spec["unit"] = "crore"
    elif re.search(r"\blakhs?\b", qn):
        spec["unit"] = "lakh"
    _period_from(q, spec)
    vals = find_values(qn, store)
    flt = {}
    for tk, c, v, g in vals:
        if store.tables[tk].cols.get(c, {}).get("role") == "dimension":
            flt.setdefault(c, set()).add(v)
    if flt:
        spec["filters"] = {c: sorted(v) for c, v in flt.items()}
    rp = rules_plan(q, store)
    focus = [c["table"] for c in rp.get("candidates", []) if c["score"] >= 1.5]
    if focus and not re.search(r"\b(mis|monthly|overall|full|complete|all)\b", qn):
        spec["tables"] = focus[:3]
    m = re.search(r"\b(with|including|covering|add(ing)?|also)\b(.+)$", q, re.I)
    if m and len(m.group(3).split()) >= 3 and not re.search(r"\b(pdf|word)\b", m.group(3).lower()):
        pass
    return build(store, spec)


def edit_report(store, spec: dict, q: str) -> dict:
    s = copy.deepcopy(spec)
    qn = normalise(q)
    changes = []
    if re.search(r"\bcrores?\b", qn):
        s["unit"] = "crore"
        changes.append("money in Rs crore")
    elif re.search(r"\blakhs?\b", qn):
        s["unit"] = "lakh"
        changes.append("money in Rs lakh")
    for m in re.finditer(r"\badd (?:a |an |one )?(?:new )?section (?:on|about|for|with|showing) (.+?)(?:$|\.| and (?=add|remove|make|in )|;)", q, re.I):
        s.setdefault("sections", []).append(m.group(1).strip())
        changes.append(f"added section '{m.group(1).strip()}'")
    for m in re.finditer(r"\bremove (?:the )?section (?:on |about )?(.+?)(?:$|\.|;)", q, re.I):
        k = m.group(1).strip().lower()
        before = len(s.get("sections", []))
        s["sections"] = [x for x in s.get("sections", []) if k not in x.lower()]
        if len(s["sections"]) < before:
            changes.append(f"removed section '{k}'")
        elif k in ("alerts", "summary", "highlights", "quality", "data quality"):
            s["include"] = [x for x in (s.get("include") or ["summary", "tables", "insights", "alerts", "quality"]) if not x.startswith(k[:5])]
            changes.append(f"removed section '{k}'")
    if re.search(r"\bonly for\b|\bjust for\b", qn):
        vals = find_values(qn, store)
        flt = {}
        for tk, c, v, g in vals:
            flt.setdefault(c, set()).add(v)
        if flt:
            s["filters"] = {c: sorted(v) for c, v in flt.items()}
            changes.append("only for " + "; ".join(f"{c} = {', '.join(v)}" for c, v in s["filters"].items()))
    p = parse_period(qn, today())
    if p and p.end != "9999-12":
        s["period_end"] = p.end
        changes.append(f"period ending {month_long(p.end)}")
    if re.search(r"\bword only|only word|just word\b", qn):
        s["formats"] = ["docx"]
        changes.append("Word only")
    elif re.search(r"\bpdf only|only pdf|just pdf\b", qn):
        s["formats"] = ["pdf"]
        changes.append("PDF only")
    if not changes:
        return result("report_edit", "No change understood", ["Try e.g. 'redo it in crore', 'add a section on complaints by category', 'only for <name>' or 'for August 2026'."], type="clarify")
    out = build(store, s)
    out["answer"].insert(0, "Rebuilt with: " + "; ".join(changes) + ". Everything else is unchanged.")
    out["intent"] = "report_edit"
    return out


# ------------------------------------------------------------------------------------- infographic
def infographic(store, q: str = "") -> dict:
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    t0 = time.time()
    tables = sorted([t for t in store.usable() if t.measures()], key=lambda t: -t.importance())
    if not tables:
        return result("report", "Nothing to show", ["No table with numbers is loaded."], type="clarify")
    M = _report_month(store, tables, {})
    cs = app_settings()
    tiles = []
    for t in tables[:6]:
        if len(tiles) >= 4:
            break
        try:
            per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if M and t.time_type in ("month", "date", "datetime") else None
            x = E.run(store, _plan(t, [{"col": t.measures()[0]}], period=per, compare="previous_year" if per else None))
            mi = x["measures"][0]
            v = x["res"]["m0"].iloc[0]
            g = x["res"]["g0"].iloc[0] if "g0" in x["res"].columns else np.nan
            tiles.append((mi["text"][:34], fmt_value(v, mi.get("unit", ""), mi.get("unit_kind", "")), f"{pct(g)} vs last year" if pd.notna(g) else t.label[:30]))
        except E.PlanError:
            continue
    main = tables[0]
    x_tr = None
    try:
        x_tr = E.run(store, _plan(main, [{"col": main.measures()[0]}], grain="month" if main.time_type in ("month", "date", "datetime") else None,
                                  period={"start": add_months(M, -11), "end": M, "label": "", "kind": "range"} if M else None))
    except E.PlanError:
        pass
    d = next((c for c in main.dims() if main.cols[c].get("type") != "fy_label"), None)
    x_br = None
    if d:
        try:
            x_br = E.run(store, _plan(main, [{"col": main.measures()[0]}], group=[d], period={"start": M, "end": M, "label": month_long(M), "kind": "month"} if M else None, sort="desc"))
        except E.PlanError:
            pass
    try:
        from ..insights.engine import run_insights
        fs = [f for f in run_insights(store) if f["category"] != "data_quality"][:3]
    except Exception:
        fs = []
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.patch.set_facecolor("white")
    fig.text(0.06, 0.955, f"{cs.get('company', '')}", fontsize=11, color="#555555")
    fig.text(0.06, 0.925, f"At a glance - {month_long(M) if M else 'all data'}", fontsize=22, weight="bold", color="#1F4E79")
    for i, (lab, val, sub) in enumerate(tiles):
        x0 = 0.06 + i * 0.225
        ax = fig.add_axes([x0, 0.80, 0.205, 0.095])
        ax.axis("off")
        ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.02,rounding_size=0.06", fc="#EEF3F8", ec="none", transform=ax.transAxes))
        ax.text(0.07, 0.72, lab, fontsize=7.2, color="#333333", transform=ax.transAxes, wrap=True)
        ax.text(0.07, 0.36, val, fontsize=12.5 if len(val) < 18 else 10, weight="bold", color="#1F4E79", transform=ax.transAxes)
        ax.text(0.07, 0.1, sub, fontsize=7, color="#C73E1D" if sub.startswith("-") else "#1E7B4D", transform=ax.transAxes)
    if x_tr is not None and len(x_tr["res"]) > 1 and "__bucket" in x_tr["res"].columns:
        ax = fig.add_axes([0.08, 0.53, 0.86, 0.22])
        r = x_tr["res"]
        ax.plot(range(len(r)), r["m0"], color="#1F4E79", lw=2, marker="o", ms=3)
        ax.set_xticks(range(len(r)))
        ax.set_xticklabels([str(v)[2:] for v in r["__bucket"]], rotation=45, fontsize=7)
        ax.set_title(f"{x_tr['measures'][0]['text']} - last 12 months", fontsize=10, loc="left", weight="bold")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(alpha=0.25)
    if x_br is not None and len(x_br["res"]):
        ax = fig.add_axes([0.08, 0.27, 0.86, 0.21])
        r = x_br["res"].head(10)
        ax.barh([str(v) for v in r[d]][::-1], list(r["m0"])[::-1], color="#2E86AB")
        ax.set_title(f"{x_br['measures'][0]['text']} by {main.cols[d]['label'].lower()} - {x_br['period']['label'] if x_br.get('period') else ''}", fontsize=10, loc="left", weight="bold")
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7.5)
    fig.text(0.06, 0.225, "What stands out", fontsize=12, weight="bold", color="#1F4E79")
    y = 0.195
    for f in fs:
        txt = f"- {f['title']}"
        fig.text(0.07, y, txt[:110], fontsize=8.5, color="#222222")
        y -= 0.03
    fig.text(0.06, 0.03, f"Sources: {', '.join(t.label for t in tables[:5])}. Generated by SAHAYAK-AI {datetime.now():%d-%b-%Y %H:%M}. All figures computed from uploaded data.",
             fontsize=6.5, color="#666666")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png = OUTPUT_DIR / f"infographic_{stamp}.png"
    pdf = OUTPUT_DIR / f"infographic_{stamp}.pdf"
    fig.savefig(png, dpi=150)
    fig.savefig(pdf)
    plt.close(fig)
    return result("report", "Infographic created", [f"One-page infographic ready in {time.time() - t0:.1f} s: {len(tiles)} headline figures, a 12-month trend, a breakdown and the top findings."],
                  files=[{"name": png.name, "label": "Infographic (PNG)"}, {"name": pdf.name, "label": "Infographic (PDF)"}], preview_image=png.name)
