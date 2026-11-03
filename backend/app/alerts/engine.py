"""Alerts on any number in the data dictionary.

Rule = table + measure (+ optional grouping) per time period + condition + amber/red limits. Rules are created on the
Alerts page or by asking ("alert me when UFG % by area goes above 2"). They are evaluated after every pipeline load
and every 15 minutes; new breaches become notifications. High-severity insights also appear as automatic alerts."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime

import pandas as pd

from ..config import STATE_DIR
from ..query import engine as E
from ..query.answer import result
from ..util import clean, fmt_value

RULES = STATE_DIR / "alert_rules.json"
NOTES = STATE_DIR / "notifications.json"
_lock = threading.RLock()


def _read(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(p, v):
    p.write_text(json.dumps(clean(v), indent=1), encoding="utf-8")


def list_rules() -> list[dict]:
    return _read(RULES, [])


def save_rule(r: dict, store) -> dict:
    t = store.get(r.get("table", ""))
    if not t:
        raise ValueError("Unknown table")
    if r.get("measure") not in t.cols and r.get("measure") != "*":
        raise ValueError("Unknown measure")
    if r.get("group") and r["group"] not in t.cols:
        raise ValueError("Unknown grouping column")
    if r.get("op") not in (">", "<", ">=", "<="):
        raise ValueError("Condition must be >, <, >= or <=")
    if r.get("amber") is None and r.get("red") is None:
        raise ValueError("Give at least one limit (amber or red)")
    with _lock:
        rules = list_rules()
        r = {"id": r.get("id") or uuid.uuid4().hex[:8], "name": r.get("name") or "", "table": r["table"], "measure": r["measure"], "agg": r.get("agg") or None,
             "group": r.get("group") or "", "grain": r.get("grain") or ("month" if t.time else "none"), "op": r["op"],
             "amber": None if r.get("amber") in (None, "") else float(r["amber"]), "red": None if r.get("red") in (None, "") else float(r["red"]),
             "window": r.get("window") or "latest", "owner": r.get("owner") or "", "enabled": r.get("enabled", True),
             "filters": r.get("filters") or [], "created": datetime.now().isoformat(timespec="seconds")}
        if not r["name"]:
            mi = E.measure_info(t, {"col": r["measure"], "agg": r["agg"]})
            r["name"] = f"{mi['text']}" + (f" by {t.cols[r['group']].get('label', r['group']).lower()}" if r["group"] else "") + f" {r['op']} {r['red'] if r['red'] is not None else r['amber']:g}"
        rules = [x for x in rules if x["id"] != r["id"]] + [r]
        _write(RULES, rules)
        return r


def delete_rule(rid: str) -> bool:
    with _lock:
        rules = list_rules()
        n = len(rules)
        _write(RULES, [x for x in rules if x["id"] != rid])
        return len(list_rules()) < n


def _hit(v, op, lim) -> bool:
    if lim is None or v is None or pd.isna(v):
        return False
    return {">": v > lim, ">=": v >= lim, "<": v < lim, "<=": v <= lim}[op]


def evaluate_rule(store, r: dict) -> list[dict]:
    t = store.get(r["table"])
    if not t or not r.get("enabled", True):
        return []
    plan = {"table": t.key, "measures": [{"col": r["measure"], **({"agg": r["agg"]} if r.get("agg") else {})}], "group": [r["group"]] if r.get("group") else [],
            "grain": r["grain"] if t.time and r.get("grain") not in (None, "none") else None, "filters": r.get("filters") or [], "having": [], "period": None,
            "compare": None, "sort": None, "limit": None, "share": False, "chart": "auto", "mode": "aggregate"}
    try:
        x = E.run(store, plan)
    except E.PlanError:
        return []
    res = x["res"]
    mi = x["measures"][0]
    if "__bucket" in res.columns and len(res):
        last = res["__bucket"].max()
        if r.get("window") == "latest":
            res = res[res["__bucket"] == last]
    out = []
    for _, row in res.iterrows():
        v = row["m0"]
        sev = "Red" if _hit(v, r["op"], r.get("red")) else "Amber" if _hit(v, r["op"], r.get("amber")) else None
        if not sev:
            continue
        ent = str(row[r["group"]]) if r.get("group") else "All"
        per = str(row["__bucket"]) if "__bucket" in res.columns else "all data"
        lim = r["red"] if sev == "Red" else r["amber"]
        out.append({"key": f"{r['id']}|{ent}|{per}", "rule_id": r["id"], "severity": sev, "alert": r["name"], "entity": ent, "period": per,
                    "value": fmt_value(v, mi.get("unit", ""), mi.get("unit_kind", "")), "raw": float(v), "limit": lim, "owner": r.get("owner", ""),
                    "evidence": f"{mi['text']} = {fmt_value(v, mi.get('unit', ''), mi.get('unit_kind', ''))} vs limit {r['op']} {lim:g} ({t.label})", "table": t.key, "auto": False})
    return out


def evaluate(store, include_auto: bool = True) -> list[dict]:
    out = []
    for r in list_rules():
        out += evaluate_rule(store, r)
    if include_auto:
        try:
            from ..insights.engine import run_insights
            for f in run_insights(store):
                if f["severity"] == "high" and f["category"] in ("anomaly_window", "member_change", "spike", "deterioration", "peer_outlier"):
                    out.append({"key": f"auto|{f['id']}", "rule_id": "auto", "severity": "Amber" if f["category"] == "peer_outlier" else "Red", "alert": f["title"],
                                "entity": f["entity"] or f["table_label"], "period": f["period"] or "-", "value": "", "owner": "", "evidence": f["detail"][0].replace("**", ""),
                                "table": f["table"], "auto": True})
        except Exception:
            pass
    return sorted(out, key=lambda a: (a["severity"] != "Red", a["auto"], a["alert"]))


def notifications() -> list[dict]:
    return _read(NOTES, [])


def mark_read() -> None:
    with _lock:
        ns = notifications()
        for n in ns:
            n["read"] = True
        _write(NOTES, ns)


def run_rules(store) -> int:
    """Evaluate rules and add new breaches as notifications. Returns how many are new."""
    with _lock:
        ns = notifications()
        seen = {n["key"] for n in ns}
        new = 0
        for a in evaluate(store, include_auto=False):
            if a["key"] in seen:
                continue
            ns.append({"key": a["key"], "ts": datetime.now().isoformat(timespec="seconds"), "read": False, "severity": a["severity"],
                       "message": f"{a['severity']}: {a['alert']} - {a['entity']} ({a['period']}): {a['value']}"})
            new += 1
        _write(NOTES, ns[-300:])
        return new


COND = re.compile(r"\b(above|over|more than|greater than|exceeds?|crosses|goes above|rises above|>=|>|below|under|less than|falls below|drops below|goes below|<=|<)\s*(rs\.?\s*)?(-?[\d,]*\.?\d+)\s*(%|percent)?")


def rule_from_question(store, q: str) -> dict:
    from ..query.understand import rules_plan
    rp = rules_plan(q, store)
    m = COND.search(q.lower())
    if not rp.get("plan") or not m:
        return result("alert", "Alert not created", ["I need a number and a limit, for example: 'Alert me when UFG % by area goes above 2' or 'Notify me if daily kg sold drops below 2500'."], type="clarify")
    p = rp["plan"]
    t = store.get(p["table"])
    w = m.group(1)
    op = "<" if w in ("below", "under", "less than", "falls below", "drops below", "goes below", "<") else "<=" if w == "<=" else ">=" if w == ">=" else ">"
    lim = float(m.group(3).replace(",", ""))
    meas = p["measures"][0]
    grain = p.get("grain") or ("day" if re.search(r"\b(daily|day|per day)\b", q.lower()) and t.time_type in ("date", "datetime") else "month" if t.time else "none")
    grp = (p.get("group") or [""])[0]
    if not grp and re.search(r"\b(any|each|every)\s+([a-z]+)", q.lower()):
        from ..query.understand import _resolve_dim
        grp = _resolve_dim(t, re.search(r"\b(any|each|every)\s+([a-z]+)", q.lower()).group(2), t.dims(), store) or ""
    r = save_rule({"table": t.key, "measure": meas["col"], "agg": meas.get("agg"), "group": grp, "grain": grain, "op": op, "red": lim, "window": "latest",
                   "filters": [f for f in p.get("filters", []) if f["op"] in ("in", "not_in")]}, store)
    now = evaluate_rule(store, {**r, "window": "all"})
    run_rules(store)
    ans = [f"Alert created: **{r['name']}** (checked per {grain if grain != 'none' else 'whole table'}" + (f", for each {t.cols[grp].get('label', grp).lower()}" if grp else "") + ").",
           f"It is checked after every data load and every 15 minutes; breaches appear under Alerts and as notifications."]
    if now:
        ans.append(f"It would already have triggered **{len(now)}** time(s) in the loaded data, most recently: " + "; ".join(f"{a['entity']} {a['period']} = {a['value']}" for a in now[-3:]) + ".")
    else:
        ans.append("Nothing in the loaded data breaches it right now.")
    return result("alert", "Alert set", ans, table={"columns": ["Entity", "Period", "Value"], "rows": [[a["entity"], a["period"], a["value"]] for a in now[-20:]],
                                                    "caption": "Past breaches in the loaded data", "total_rows": len(now)} if now else None)
