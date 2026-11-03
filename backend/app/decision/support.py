"""Decision support: what to act on first, what-if scenarios and plan / budget vs actual - all from computed figures."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from ..query import engine as E
from ..query.answer import result
from ..query.understand import COND_RE, normalise, rules_plan
from ..util import fmt_value, pct

ACTION = {
    "anomaly_window": "Investigate on site: find the cause of the abnormal period for {entity} ({period}) and fix it.",
    "member_change": "Find out what changed for {entity} in {period} (operations, recording or a one-off).",
    "spike": "Find the root cause of the spike for {entity} ({period}) - often a system change, a policy change or an outage.",
    "deterioration": "Review {entity} now: it has been getting worse every period. Agree a recovery plan with an owner and a date.",
    "peer_outlier": "Compare {entity} with its peers and correct what is different (equipment, behaviour, process or data).",
    "plan_gap": "Re-check the plan for {entity}: the gap is consistent, so the plan itself may be wrong.",
    "trend": "Plan for the trend in {measure} to continue - capacity, budget and targets.",
    "step_change": "Confirm the step change in {measure} from {period} was expected (e.g. a price revision) and update targets.",
    "stopped_member": "Check whether {entity} closed or its data feed stopped.",
    "data_quality": "Ask the source team to fix the export of {table}: {entity}.",
    "concentration": "Keep a close watch on {entity}; results depend heavily on it.",
    "seasonality": "Use month-by-month targets for {measure}.",
    "new_member": "Report growth like-for-like: {entity} is new.",
}
W = {"high": 3, "medium": 2, "low": 1}


def _money_rate(store, excess_col: str, table) -> tuple[float, str] | None:
    """A price to value excess volume: any ratio measure money / same-kind volume in the loaded data."""
    kind = table.cols.get(excess_col, {}).get("unit_kind")
    if kind not in ("volume", "mass"):
        return None
    for t in store.usable():
        for c, e in t.cols.items():
            r = e.get("ratio")
            if e.get("agg") == "ratio" and r and t.cols.get(r["num"], {}).get("unit_kind") == "money" and t.cols.get(r["den"], {}).get("unit_kind") == kind:
                num, den = t.df[r["num"]].sum(), t.df[r["den"]].sum()
                scale = t.cols[r["num"]].get("scale") or 1.0
                if den:
                    rate_rs = num * scale / den  # Rs per unit of volume
                    return rate_rs, f"average {e.get('label', c)} across {t.label}"
    return None


def top_actions(store, q: str = "") -> dict:
    from ..alerts.engine import evaluate
    from ..insights.engine import run_insights
    fs = [f for f in run_insights(store) if f["category"] not in ("seasonality", "new_member")]
    alerts = [a for a in evaluate(store, include_auto=False) if a["severity"] == "Red"]
    items = []
    for a in alerts:
        items.append({"score": 10, "what": f"{a['alert']}: {a['entity']} ({a['period']}) = {a['value']}", "action": f"Act on the red alert for {a['entity']}" + (f" (owner: {a['owner']})" if a.get("owner") else "") + ".",
                      "impact": "", "source": a["evidence"]})
    for f in fs:
        imp = ""
        sc = W[f["severity"]] * f["impact"]
        if f.get("excess") and f.get("excess_col"):
            t = store.get(f["table"])
            mr = _money_rate(store, f["excess_col"], t) if t else None
            if mr:
                rs = f["excess"] * mr[0]
                imp = f"~Rs {rs / 1e5:,.2f} lakh (excess valued at {mr[1]})"
                sc += min(10, rs / 1e5)
        items.append({"score": sc, "what": f["title"], "action": ACTION.get(f["category"], "Review {entity}.").format(entity=f["entity"] or f["table_label"], period=f["period"] or "the period",
                                                                                                                     measure=(f["measure_label"] or "").lower(), table=f["table_label"]),
                      "impact": imp, "source": f["detail"][0].replace("**", "")})
    items.sort(key=lambda x: -x["score"])
    top = items[:5]
    if not top:
        return result("actions", "Nothing urgent", ["No red alerts and no significant findings in the loaded data."])
    ans = ["Ranked by size and severity (red alerts first, then the largest abnormal changes). Every item links to its evidence:"]
    for i, x in enumerate(top, 1):
        ans.append(f"{i}. **{x['what']}** -> {x['action']}" + (f" Impact: {x['impact']}." if x["impact"] else ""))
    return result("actions", "What to act on first", ans, table={"columns": ["#", "Issue", "Suggested action", "Estimated impact", "Evidence"],
                                                                 "rows": [[i, x["what"], x["action"], x["impact"] or "-", x["source"][:160]] for i, x in enumerate(items[:15], 1)],
                                                                 "caption": "Prioritised issues", "total_rows": min(15, len(items))},
                  assumptions=["Impact in rupees is shown only where the data has both the excess quantity and a price for it."],
                  followups=["What unusual things do you see in the data?"])


PCT = re.compile(r"(increases?|rises?|grows?|goes up|is raised|raise|up|decreases?|falls?|drops?|goes down|is cut|cut|down|changes?)\s+(by\s+)?([-+]?\d+(\.\d+)?)\s*(%|percent)")
TO = re.compile(r"(increases?|rises?|goes up|decreases?|falls?|drops?|goes down|changes?)\s+to\s+(rs\.?\s*)?(\d[\d,]*(\.\d+)?)")


def what_if(store, q: str) -> dict:
    qn = normalise(q)
    rp = rules_plan(re.sub(r"\bwhat if\b", " ", qn), store)
    if not rp.get("plan"):
        return result("what_if", "What-if not understood", ["Say which number changes and by how much, e.g. 'What if the price increases by 5%?' or 'What if volume in one area drops by 10%?'"], type="clarify")
    p = rp["plan"]
    t = store.get(p["table"])
    m = PCT.search(qn)
    mt = TO.search(qn)
    meas = p["measures"][0]["col"]
    base_x = E.run(store, {**p, "group": [], "grain": None, "compare": None, "having": [], "share": False, "limit": None, "sort": None})
    base = float(base_x["res"]["m0"].iloc[0])
    if m:
        k = float(m.group(3)) / 100 * (-1 if re.match(r"(decreas|fall|drop|down|cut)", m.group(1)) else 1)
    elif mt:
        k = float(mt.group(3).replace(",", "")) / base - 1 if base else 0
    else:
        return result("what_if", "What-if not understood", ["Give the size of the change, e.g. 'by 5%' or 'to 45'."], type="clarify")
    mi = base_x["measures"][0]
    rows = [[mi["text"], fmt_value(base, mi["unit"], mi["unit_kind"]), fmt_value(base * (1 + k), mi["unit"], mi["unit_kind"]), pct(k * 100)]]
    notes = []
    period = base_x["period"]["label"] if base_x.get("period") else "all loaded data"
    # propagate through formulas found in the data
    for r in t.entry.get("relations", []):
        tgt = r["target"]
        if tgt == meas:
            continue
        if meas in r["parts"] or (t.cols.get(meas, {}).get("ratio") and t.cols[meas]["ratio"]["num"] == tgt):
            tmi = E.measure_info(t, {"col": tgt})
            if tmi["agg"] not in ("sum",):
                continue
            tx = E.run(store, {**p, "measures": [{"col": tgt}], "group": [], "grain": None, "compare": None, "having": [], "share": False, "limit": None, "sort": None})
            tb = float(tx["res"]["m0"].iloc[0])
            if r["kind"] == "product":
                tn = tb * (1 + k)
            elif r["kind"] in ("sum", "difference"):
                part = E.run(store, {**p, "measures": [{"col": meas}], "group": [], "grain": None, "compare": None, "having": [], "share": False, "limit": None, "sort": None})
                delta = float(part["res"]["m0"].iloc[0]) * k * (1 if r["kind"] == "sum" or r["parts"][0] == meas else -1)
                tn = tb + delta
            else:
                continue
            rows.append([tmi["text"], fmt_value(tb, tmi["unit"], tmi["unit_kind"]), fmt_value(tn, tmi["unit"], tmi["unit_kind"]), pct((tn / tb - 1) * 100 if tb else 0)])
            notes.append(f"Uses the formula found in the data: {r['formula']}.")
    if t.cols.get(meas, {}).get("agg") == "ratio":
        r = t.cols[meas]["ratio"]
        num_mi = E.measure_info(t, {"col": r["num"]})
        tx = E.run(store, {**p, "measures": [{"col": r["num"]}], "group": [], "grain": None, "compare": None, "having": [], "share": False, "limit": None, "sort": None})
        nb = float(tx["res"]["m0"].iloc[0])
        rows.append([num_mi["text"], fmt_value(nb, num_mi["unit"], num_mi["unit_kind"]), fmt_value(nb * (1 + k), num_mi["unit"], num_mi["unit_kind"]), pct(k * 100)])
        notes.append(f"{mi['label']} = {r['num']} / {r['den']}, so with the same {r['den']} the total {r['num']} moves by the same %.")
    seen, uniq = set(), []
    for r_ in rows:
        if r_[0] not in seen:
            seen.add(r_[0])
            uniq.append(r_)
    rows = uniq
    ans = [f"Scenario for **{period}**: {mi['text'].lower()} {'up' if k >= 0 else 'down'} {abs(k) * 100:.1f}% -> {rows[0][2]} (from {rows[0][1]})."]
    if len(rows) > 1:
        ans += [f"{r_[0]}: {r_[1]} -> **{r_[2]}** ({r_[3]})." for r_ in rows[1:]]
    ans.append("Everything else is held constant (no change in demand or mix). Use it as a sizing estimate, not a forecast.")
    return result("what_if", "What-if scenario", ans, table={"columns": ["Figure", "Now", "Scenario", "Change"], "rows": rows, "caption": f"Scenario on {period}", "total_rows": len(rows)},
                  notes=notes, sources=[{"file": ", ".join(t.entry.get("sources", [])[:2]), "origin": t.label, "filter": period}])


PLANW = re.compile(r"(budget|target|plan|planned|forecast|estimate)")


def plan_vs_actual(store, q: str) -> dict | None:
    """Pair a plan/budget/target measure with the matching actual measure (same table, or another table with
    the same unit and time grain) and compare period by period."""
    pairs = []
    for t in store.usable():
        for c in t.measures():
            if not PLANW.search(c) or not t.time:
                continue
            core = PLANW.sub("", c).strip("_")
            unit = t.cols[c].get("unit")
            # same table
            for a in t.measures():
                if a != c and not PLANW.search(a) and (core and core in a or t.cols[a].get("unit") == unit):
                    pairs.append((t, c, t, a))
            # other tables with same unit and a monthly time column
            for t2 in store.usable():
                if t2.key == t.key or not t2.time:
                    continue
                for a in t2.measures():
                    if not PLANW.search(a) and unit and t2.cols[a].get("unit") == unit and t2.cols[a].get("agg") == "sum":
                        pairs.append((t, c, t2, a))
    if not pairs:
        return None
    best = None
    for tb, cb, ta, ca in pairs:
        pb = E.run(store, {"table": tb.key, "measures": [{"col": cb}], "group": [], "grain": "month", "filters": [], "having": [], "period": None, "compare": None,
                           "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"})["res"]
        lo, hi = pb["__bucket"].min(), pb["__bucket"].max()
        pa = E.run(store, {"table": ta.key, "measures": [{"col": ca}], "group": [], "grain": "month", "filters": [], "having": [], "period": {"start": lo, "end": hi, "label": "", "kind": "range"},
                           "compare": None, "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"})["res"]
        m = pb.merge(pa, on="__bucket", suffixes=("_b", "_a"))
        if len(m) == 0:
            continue
        ratio = (m["m0_a"] / m["m0_b"]).median()
        score = abs(np.log(ratio)) if ratio > 0 else 9
        qbonus = -1 if any(w in q.lower() for w in re.findall(r"[a-z]{4,}", (ca + " " + ta.label).lower())) else 0
        if best is None or score + qbonus < best[0]:
            best = (score + qbonus, tb, cb, ta, ca, m)
    if not best or best[0] > 1.5:
        return None
    _, tb, cb, ta, ca, m = best
    mi = E.measure_info(ta, {"col": ca})
    m = m.rename(columns={"__bucket": "month"})
    m["var"] = m["m0_a"] - m["m0_b"]
    m["pct"] = m["m0_a"] / m["m0_b"] * 100
    miss = m[m["pct"] < 100]
    ans = [f"Compared **{ta.cols[ca].get('label', ca)}** ({ta.label}) with **{tb.cols[cb].get('label', cb)}** ({tb.label}) for {len(m)} month(s), {m['month'].min()} to {m['month'].max()}.",
           f"Total: actual {fmt_value(m['m0_a'].sum(), mi['unit'], mi['unit_kind'])} vs plan {fmt_value(m['m0_b'].sum(), mi['unit'], mi['unit_kind'])} = **{m['m0_a'].sum() / m['m0_b'].sum() * 100:.1f}%** achieved."]
    if len(miss):
        ans.append("Missed in: " + "; ".join(f"{r.month} ({r.pct:.1f}%, short by {fmt_value(-r.var, mi['unit'], mi['unit_kind'])})" for r in miss.itertuples()) + ".")
    else:
        ans.append("Plan was met or beaten every month.")
    rows = [[r.month, round(r.m0_b, 2), round(r.m0_a, 2), round(r.var, 2), round(r.pct, 1)] for r in m.itertuples()]
    return result("plan_vs_actual", "Actual vs plan", ans, table={"columns": ["Month", "Plan", "Actual", "Difference", "% achieved"], "rows": rows, "caption": "By month", "total_rows": len(rows)},
                  chart={"type": "bar", "title": "Actual vs plan", "x": m["month"].tolist(), "series": [{"name": "Plan", "data": [round(v, 2) for v in m["m0_b"]]},
                                                                                                           {"name": "Actual", "data": [round(v, 2) for v in m["m0_a"]]}]},
                  assumptions=[f"Plan column: {tb.label}.{cb}; actual column: {ta.label}.{ca} (same unit {mi['unit'] or ''}). Change the pairing by renaming columns on the Data page if this is wrong."],
                  sources=[{"file": ", ".join(tb.entry.get("sources", [])[:1] + ta.entry.get("sources", [])[:1]), "origin": f"{tb.label} + {ta.label}"}])
