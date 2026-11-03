"""Deck specs built from the query engine; rendered by render.py."""
from __future__ import annotations

import copy
import re
from datetime import datetime

import numpy as np
import pandas as pd

from ..config import app_settings
from ..query import engine as E
from ..query.answer import result
from ..query.periods import add_months, describe, month_long
from ..query.understand import normalise, rules_plan
from ..util import fmt_value, pct
from .render import C, S, render

NF = lambda vals: "#,##0" if np.nanmax(np.abs(np.asarray(vals, dtype=float))) >= 100 else "#,##0.00"  # noqa: E731


def _plan(t, measures, **kw):
    p = {"table": t.key, "measures": measures, "group": [], "grain": None, "filters": [], "having": [], "period": None, "compare": None, "sort": None,
         "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
    p.update(kw)
    return p


def _f(v, mi):
    return fmt_value(v, mi.get("unit", ""), mi.get("unit_kind", ""))


def _latest_month(t) -> str | None:
    tr = t.entry.get("time_range")
    if not tr or t.time_type not in ("month", "date", "datetime"):
        return None
    last = str(tr[1])[:7]
    if t.time_type in ("date", "datetime"):
        d = pd.Timestamp(str(tr[1])[:10])
        if d.day < (d + pd.offsets.MonthEnd(0)).day - 2:
            last = add_months(last, -1)
    return last


def trend_slide(store, t, m: str, filters=None, period=None, title=None, band=None) -> dict | None:
    grain = "month" if t.time_type == "month" or (t.entry.get("grain") in ("day", "hour") and not period) else "day"
    if period and period.get("d0") is None and t.entry.get("grain") in ("day", "hour"):
        grain = "day"
    try:
        x = E.run(store, _plan(t, [{"col": m}], grain=grain, filters=filters or [], period=period))
    except E.PlanError:
        return None
    r = x["res"]
    if len(r) < 2:
        return None
    mi = x["measures"][0]
    ys = r["m0"].tolist()
    xs = [str(v) for v in r["__bucket"]]
    i_max, i_min = int(np.nanargmax(ys)), int(np.nanargmin(ys))
    ch = (ys[-1] / ys[0] - 1) * 100 if ys[0] else np.nan
    ft = "; ".join(f"{f['col']} = {', '.join(map(str, f['values']))}" for f in (filters or []) if f["op"] == "in")
    bullets = [f"{xs[0]} -> {xs[-1]}: {_f(ys[0], mi)} -> {_f(ys[-1], mi)}" + (f" ({pct(ch)})" if np.isfinite(ch) else ""),
               f"Highest: {xs[i_max]} ({_f(ys[i_max], mi)})", f"Lowest: {xs[i_min]} ({_f(ys[i_min], mi)})"]
    if band:
        bullets.insert(0, f"Abnormal period: {band}")
    notes = f"{mi['text']} per {grain}" + (f" for {ft}" if ft else "") + f", {x['period']['label'] if x.get('period') else 'all loaded data'}. Source: {t.label} ({', '.join(t.entry.get('sources', [])[:2])}). " \
            f"Each value is computed in code and cross-checked with SQL. Highest {xs[i_max]} at {_f(ys[i_max], mi)}; lowest {xs[i_min]} at {_f(ys[i_min], mi)}."
    return S("chart", title or f"{mi['text']} over time" + (f" - {ft}" if ft else ""), bullets,
             C("line", xs, {mi["text"][:40]: [round(v, 4) for v in ys]}, f"{mi['text']} per {grain}", NF(ys)), notes)


def breakdown_slide(store, t, m: str, dim: str, filters=None, period=None, title=None, kind=None, highlight=None) -> dict | None:
    try:
        x = E.run(store, _plan(t, [{"col": m}], group=[dim], filters=filters or [], period=period, share=True, sort="desc",
                               compare="previous_year" if period and t.time else None))
    except E.PlanError:
        return None
    r = x["res"].dropna(subset=["m0"])
    if len(r) < 2:
        return None
    mi = x["measures"][0]
    kind = kind or ("pie" if "s0" in r.columns and len(r) <= 6 else "bar")
    top = r.head(12)
    lab = t.cols[dim].get("label", dim)
    bullets = [f"Highest: {top[dim].iloc[0]} ({_f(top['m0'].iloc[0], mi)}" + (f", {top['s0'].iloc[0]:.1f}% of total" if "s0" in top.columns else "") + ")",
               f"Lowest: {r[dim].iloc[-1]} ({_f(r['m0'].iloc[-1], mi)})"]
    if "g0" in r.columns and r["g0"].notna().any():
        rr = r.dropna(subset=["g0"]).sort_values("g0")
        bullets.append(f"Fastest growth vs last year: {rr[dim].iloc[-1]} ({pct(rr['g0'].iloc[-1])}); slowest: {rr[dim].iloc[0]} ({pct(rr['g0'].iloc[0])})")
    if highlight:
        hv = r[r[dim].astype(str) == str(highlight)]
        if len(hv):
            others = r[r[dim].astype(str) != str(highlight)]["m0"].median()
            bullets.insert(0, f"{highlight}: {_f(hv['m0'].iloc[0], mi)} vs {_f(others, mi)} median of the others")
    notes = f"{mi['text']} by {lab.lower()}, {x['period']['label'] if x.get('period') else 'all loaded data'}. Source: {t.label}. " + \
            "; ".join(f"{a}: {_f(b, mi)}" for a, b in zip(top[dim], top["m0"])) + "."
    return S("chart", title or f"{mi['text']} by {lab.lower()}", bullets,
             C(kind, [str(v) for v in top[dim]], {mi["text"][:40]: [round(v, 4) for v in top["m0"]]}, f"{mi['text']} by {lab.lower()}" + (f", {x['period']['label']}" if x.get("period") else ""),
               NF(top["m0"])), notes)


def _insight_slide(fs, title="What stands out") -> dict:
    return S("bullets", title, [f"{f['title']}" for f in fs[:6]], None, " ".join(f["detail"][0].replace("**", "") for f in fs[:6]))


def _alerts_slide(store) -> dict | None:
    from ..alerts.engine import evaluate
    al = evaluate(store)[:10]
    if not al:
        return None
    return S("table", "Alerts", [], None, f"{len(al)} alert(s): {sum(a['severity'] == 'Red' for a in al)} red. From alert rules and automatic checks.",
             {"columns": ["Severity", "Alert", "Item", "Period"], "rows": [[a["severity"], a["alert"][:60], a["entity"][:30], str(a["period"])[:25]] for a in al]})


def _actions_slide(store, filt=None) -> dict:
    from ..decision.support import top_actions
    a = top_actions(store)
    rows = (a.get("table") or {}).get("rows", [])
    if filt:
        sel = [r for r in rows if filt.lower() in (r[1] + r[2]).lower()] or rows
    else:
        sel = rows
    return S("bullets", "Recommended actions", [f"{r[2]}" + (f" (impact {r[3]})" if r[3] and r[3] != "-" else "") for r in sel[:5]], None,
             "Actions are ranked by severity and size; each links to an alert or a finding with its evidence. " + " ".join(f"{r[1]}: {r[4]}" for r in sel[:5]))


def overview_deck(store, n: int = 10, template: str | None = None) -> dict:
    cs = app_settings()
    tables = sorted([t for t in store.usable() if t.measures()], key=lambda t: -t.importance())[:4]
    M = max([m for m in (_latest_month(t) for t in tables) if m], default=None)
    slides = [S("title", f"{cs.get('company', '')} - business review", subtitle=f"{month_long(M) if M else ''} | prepared by SAHAYAK-AI {datetime.now():%d-%b-%Y}")]
    rows = []
    for t in tables:
        m = t.measures()[0]
        try:
            per = {"start": M, "end": M, "label": month_long(M), "kind": "month"} if M and t.time_type in ("month", "date", "datetime") else None
            x = E.run(store, _plan(t, [{"col": m}], period=per, compare="previous_year" if per else None))
            mi = x["measures"][0]
            g = x["res"]["g0"].iloc[0] if "g0" in x["res"].columns else np.nan
            rows.append([t.label, mi["text"], _f(x["res"]["m0"].iloc[0], mi), pct(g) if pd.notna(g) else "-"])
        except E.PlanError:
            continue
    if rows:
        slides.append(S("table", "Key numbers", [], None, f"Headline figure from each table for {month_long(M) if M else 'all data'}, with change vs the same period last year.",
                        {"columns": ["Area of business", "Figure", "Value", "vs last year"], "rows": rows}))
    for t in tables[:3]:
        m = t.measures()[0]
        per = {"start": add_months(M, -23), "end": M, "label": "", "kind": "range"} if M and t.time_type in ("month", "date", "datetime") else None
        s = trend_slide(store, t, m, period=per, title=f"{t.label}: {t.cols[m]['label']} trend")
        if s:
            slides.append(s)
        d = next((c for c in t.dims() if t.cols[c].get("type") != "fy_label" and c != "unit_reported"), None)
        if d:
            s = breakdown_slide(store, t, m, d, period={"start": M, "end": M, "label": month_long(M), "kind": "month"} if M and t.time else None,
                                title=f"{t.label}: by {t.cols[d]['label'].lower()}")
            if s:
                slides.append(s)
    try:
        from ..insights.engine import run_insights
        fs = [f for f in run_insights(store) if f["category"] != "data_quality"]
        if fs:
            slides.append(_insight_slide(fs))
    except Exception:
        pass
    a = _alerts_slide(store)
    if a:
        slides.append(a)
    slides.append(_actions_slide(store))
    slides.append(S("table", "Data used", [], None, "Tables loaded through the SAHAYAK-AI pipeline with their quality score.",
                    {"columns": ["Table", "Rows", "Covers", "Quality"], "rows": [[t.label, f"{len(t.df):,}", " to ".join(str(x)[:10] for x in (t.entry.get('time_range') or [])) or "-",
                                                                                  f"{t.entry.get('quality', 0):.0f}%"] for t in store.usable()[:10]]}))
    slides = _fit(slides, n)
    return {"kind": "overview", "title": slides[0]["title"], "slides": slides, "template": template}


def _fit(slides: list, n: int) -> list:
    if len(slides) <= n:
        return slides
    keep = [slides[0]] + slides[1:-1][: max(0, n - 2)] + [slides[-1]] if n >= 2 else slides[:1]
    return keep[:n]


def topic_deck(store, topic: str, n: int = 5, template: str | None = None) -> dict | None:
    rp = rules_plan(topic, store)
    if not rp.get("plan") or rp.get("confidence", 0) < 0.45:
        return None
    p = rp["plan"]
    t = store.get(p["table"])
    m = p["measures"][0]["col"]
    if m in (None, "*") or m not in t.cols:
        m = t.measures()[0] if t.measures() else None
    if not m:
        return None
    filters = p.get("filters", [])
    ent = next((str(f["values"][0]) for f in filters if f["op"] == "in" and f["values"]), "")
    dimf = next((f["col"] for f in filters if f["op"] == "in"), None)
    period = p.get("period")
    title = re.sub(r"\s+", " ", topic).strip().rstrip("?.")
    from ..query.understand import find_values
    for _, _, v, g in find_values(normalise(title), store):
        title = re.sub(rf"\b{re.escape(g)}\b", str(v), title, flags=re.I)
    title = re.sub(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", lambda mm: mm.group(1).capitalize(), title)
    title = re.sub(r"^(the|a|an)\s+", "", title, flags=re.I)
    title = title[:1].upper() + title[1:]
    slides = [S("title", title[:90], subtitle=f"{t.label} | prepared by SAHAYAK-AI {datetime.now():%d-%b-%Y}")]
    # situation: the member vs its peers
    dim = dimf or next((c for c in t.dims() if t.cols[c].get("type") != "fy_label"), None)
    if dim:
        s = breakdown_slide(store, t, m, dim, filters=[f for f in filters if f["col"] != dim], period=period, title="The situation", kind="bar", highlight=ent or None)
        if s:
            slides.append(s)
    # trend around the period, with the abnormal window from insights
    from ..insights.engine import run_insights
    fs = [f for f in run_insights(store) if f["table"] == t.key and (not ent or f["entity"] == ent)]
    win = next((f for f in fs if f["category"] == "anomaly_window"), None)
    tp = None
    if period:
        tp = {"start": add_months(period["start"], -2), "end": add_months(period["end"], 1), "label": "", "kind": "range"}
    s = trend_slide(store, t, m, filters=filters, period=tp, title="What happened, day by day" if t.entry.get("grain") in ("day", "hour") else "What happened over time",
                    band=win["period"] if win else None)
    if s:
        slides.append(s)
    if fs:
        det = []
        for f in fs[:4]:
            det += [d.replace("**", "") for d in f["detail"][:2]]
        slides.append(S("bullets", "What we found", det[:6], None, "Findings from automatic checks (trend, abnormal periods, peers). " + " ".join(det[:6])))
    act = _actions_slide(store, ent or t.label)
    act["title"] = "What we should do"
    act["bullets"] = act["bullets"][:3] + [f"Track {t.cols[m]['label'].lower()} until it is back to normal and set an alert so a repeat is caught early",
                                           "Agree owners and dates in this meeting; review at the next one"]
    slides.append(act)
    if n > len(slides):
        d2 = next((c for c in t.dims() if c != dim and t.cols[c].get("type") != "fy_label"), None)
        if d2:
            s = breakdown_slide(store, t, m, d2, filters=filters, period=period)
            if s:
                slides.insert(-2, s)
    slides = _fit(slides, n)
    return {"kind": "topic", "title": title, "slides": slides, "template": template, "topic": topic}


def summarize(spec: dict) -> list[str]:
    return [f"{i}. {s['title']}" + (f" [{s['chart']['type']} chart]" if s.get("chart") else (" [table]" if s.get("table") else "")) for i, s in enumerate(spec["slides"], 1)]


def _deliver(spec: dict, name: str | None = None, msg: list[str] | None = None) -> dict:
    path = render(spec, spec.get("template"), name=name or f"Deck_{re.sub(r'[^A-Za-z0-9]+', '_', spec['title'])[:40]}_{datetime.now():%Y%m%d_%H%M%S}.pptx")
    ans = msg or [f"**{len(spec['slides'])}-slide deck** ready: {spec['title']}." + (" Uses your uploaded template." if spec.get("template") else ""),
                  "Charts are native PowerPoint charts (editable), and every slide has speaker notes with the numbers and their source."]
    out = result("deck", "Deck created", ans, files=[{"name": path.name, "label": "PowerPoint (.pptx)"}], outline=summarize(spec))
    out["deck_spec"] = spec
    return out


def deck_from_request(store, q: str, sess: dict | None = None) -> dict:
    qn = normalise(q)
    m = re.search(r"\b(\d{1,2})\s*(?:-\s*)?slides?\b", qn)
    n = int(m.group(1)) if m else None
    tpl = None
    if re.search(r"\b(template|corporate|our (format|style|branding|theme)|company format|brand)\b", qn):
        if store.templates:
            tpl = list(store.templates.values())[-1]
    topic = re.sub(r"^.*?\b(slides?|deck|presentation|ppt|pptx|powerpoint)\b\s*(on|about|for|explaining|covering|regarding|showing)?\s*", "", qn).strip()
    topic = re.sub(r"\b(using|in|with) (our|the) (corporate )?(template|format|branding|theme)\b", "", topic).strip()
    spec = None
    if topic and len(topic.split()) >= 2 and not re.search(r"\b(board|review|overview|summary|monthly|business|management|all)\b", topic):
        spec = topic_deck(store, topic, n or 5, tpl)
    if spec is None:
        spec = overview_deck(store, n or 10, tpl)
    out = _deliver(spec)
    if tpl is None and re.search(r"\btemplate\b", qn):
        out["answer"].append("No template is loaded, so the standard layout was used. Upload your .potx/.pptx on the Pipeline page to use it.")
    return out


def edit_deck(store, spec: dict, instruction: str) -> dict:
    new = copy.deepcopy(spec)
    q = instruction.lower()
    changes = []
    m = re.search(r"(?:replace|change|convert|make|swap) (?:the |all )?(?:charts? on slide (\d+)|(pie|bar|line|column|doughnut)(?: charts?)?)(?: chart)? (?:with|to|into) (?:a |an )?(pie|bar|line|column|doughnut)", q)
    if m:
        k, a, b = m.group(1), m.group(2), m.group(3)
        for i, s in enumerate(new["slides"], 1):
            ch = s.get("chart")
            if not ch:
                continue
            if (k and int(k) == i) or (a and (ch["type"] == a or (a in ("bar", "column") and ch["type"] in ("bar", "column")))):
                old = ch["type"]
                ch["type"] = b
                changes.append(f"Slide {i} '{s['title']}': {old} chart -> {b} chart (same data)")
        if not changes:
            changes.append("No matching chart found - nothing replaced")
    for mm in re.finditer(r"add (?:a |one |another )?(?:new )?slide (?:on|about|for|covering|showing) (.+?)(?:$|[.;]| and (?=remove|change|replace|rename))", q):
        topic = mm.group(1).strip()
        rp = rules_plan(topic, store)
        sl = None
        if rp.get("plan"):
            p = rp["plan"]
            t = store.get(p["table"])
            col = p["measures"][0]["col"] if p["measures"][0]["col"] not in (None, "*") else (t.measures()[0] if t.measures() else None)
            if col:
                if p.get("group"):
                    sl = breakdown_slide(store, t, col, p["group"][0], filters=p.get("filters"), period=p.get("period"), title=topic[:1].upper() + topic[1:])
                else:
                    sl = trend_slide(store, t, col, filters=p.get("filters"), period=p.get("period"), title=topic[:1].upper() + topic[1:]) if t.time else None
                    if sl is None and t.dims():
                        sl = breakdown_slide(store, t, col, t.dims()[0], filters=p.get("filters"), period=p.get("period"), title=topic[:1].upper() + topic[1:])
        if sl is None:
            changes.append(f"Could not build a slide on '{topic}' from the loaded data")
            continue
        pos = len(new["slides"])
        if new["slides"] and re.match(r"(recommended actions|next steps|data used)", new["slides"][-1]["title"].lower()):
            pos -= 1
        new["slides"].insert(pos, sl)
        changes.append(f"Added slide {pos + 1} '{sl['title']}'")
    for mm in sorted({int(x) for x in re.findall(r"(?:remove|delete|drop) slide (\d+)", q)}, reverse=True):
        if 1 <= mm <= len(new["slides"]):
            changes.append(f"Removed slide {mm} '{new['slides'][mm - 1]['title']}'")
            new["slides"].pop(mm - 1)
    mm = re.search(r"(?:rename|retitle|change the title of) slide (\d+) to ['\"]?(.+?)['\"]?$", instruction, re.I)
    if mm:
        k = int(mm.group(1))
        if 1 <= k <= len(new["slides"]):
            new["slides"][k - 1]["title"] = mm.group(2).strip()
            changes.append(f"Slide {k} renamed to '{mm.group(2).strip()}'")
    if re.search(r"\b(use|apply|switch to) (our|the) (corporate )?template\b", q) and store.templates:
        new["template"] = list(store.templates.values())[-1]
        changes.append("Corporate template applied")
    if not changes:
        return result("deck_edit", "No change made", ["I could not identify a specific change. Try 'replace the pie chart with a bar chart', 'add a slide on <topic>', "
                                                      "'remove slide 4' or 'rename slide 2 to ...'."], type="clarify")
    touched = sum(1 for c in changes if c.startswith(("Slide", "Removed", "Added")))
    out = _deliver(new, name=f"Deck_edited_{datetime.now():%Y%m%d_%H%M%S}.pptx",
                   msg=["Updated deck saved. **Only these changes were made:**"] + [f"- {c}" for c in changes] +
                       [f"All other slides are unchanged ({max(0, len(spec['slides']) - touched)} of {len(spec['slides'])} untouched)."])
    out["intent"] = "deck_edit"
    out["extra"] = {"changes": changes}
    return out
